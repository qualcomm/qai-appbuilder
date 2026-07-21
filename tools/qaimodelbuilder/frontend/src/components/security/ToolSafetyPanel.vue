<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ToolSafetyPanel — unified three-layer security/tools switch surface.
 *
 * 2026-06 security-settings unification. Backs the authoritative
 * `GET/PUT /api/security/runtime-config` route (via `useRuntimeConfig`),
 * replacing the six dead `/api/settings/*` KV sections that had no backend
 * consumer. Three layers, one switch group each:
 *
 *   Layer 1 — Tool Guard (PatternFileBroker + tool tunables). The pure-software
 *             hygiene switch (`project_skip_dirs`) hot-applies with no restart;
 *             `file_broker_enabled` is baked into the tool bridge at build →
 *             reboot. (`ssl_verify` and the proxy URL `global_proxy` moved to
 *             Settings → 🔧 App Config on 2026-07 — they are application/network
 *             settings, not file-protection ones.)
 *   Layer 2 — Policy Guard (FileGuard / PolicyCenter). Build-time → reboot.
 *
 * (Layer 3 — OS Isolation Sandbox / Windows AppContainer was removed
 * 2026-07-01 along with the persistent ACL / sandbox cleanup; the
 * `sandbox_enabled` field on the runtime-config DTO is preserved on the
 * backend for now but no UI surface drives it.)
 *
 * Decision 3B: saving a reboot-requiring switch returns `needs_reboot=true`;
 * we show the custom reboot-confirm dialog (§3.9 — no native confirm) and, on
 * accept, drive `useReboot`. The hot switches save silently with a toast.
 *
 * V2 structure note (designed > V1): all server state + the persistence /
 * hot-apply / reboot policy live behind the route + `useRuntimeConfig`; this
 * component is a thin template host that owns only the editable drafts.
 */
import { reactive, ref, onMounted, watch } from "vue";
import { useI18n } from "vue-i18n";

import { useConfirm } from "@/composables/useConfirm";
import { useDangerousCommands } from "@/composables/useDangerousCommands";
import { useReboot } from "@/composables/useReboot";
import { useRuntimeConfig, type RuntimeConfig } from "@/composables/useRuntimeConfig";

const { t } = useI18n();
const { confirm } = useConfirm();
const { requestRebootDirect } = useReboot();
const { config, fetchConfig, save } = useRuntimeConfig();

// ── Custom dangerous-command patterns (P-10) ──
// Union-only override on top of the immutable built-in floor: the operator can
// only ADD extra regex patterns, never delete a floor entry. Baked into the
// FileBroker guard closure at build time, so a save needs a restart.
const {
  builtin: dangerousBuiltin,
  extra: dangerousExtra,
  fetchPatterns: fetchDangerousPatterns,
  save: saveDangerousPatterns,
  addExtra: addDangerousExtra,
  removeExtra: removeDangerousExtra,
} = useDangerousCommands();
const newDangerousPattern = ref("");

// Local editable draft mirrored from the server config.
const draft = reactive<RuntimeConfig>({ ...config.value });
const newSkipDir = ref("");

/**
 * Always-on security floors (3c switch-tree §6.4): the baseline
 * protections the operator CANNOT disable. They do NOT read the security
 * master switch and stay enforced under permissive/disabled. This is a
 * READ-ONLY descriptive list (no backend toggle drives them), surfaced so
 * the operator can see they exist and are immutable — rendered as greyed,
 * locked rows with an "always-on" badge rather than fake toggles.
 */
const alwaysOnFloors: ReadonlyArray<{
  key: string;
  labelKey: string;
  descKey: string;
}> = [
  {
    key: "protectedPaths",
    labelKey: "toolSafety.alwaysOn.protectedPathsLabel",
    descKey: "toolSafety.alwaysOn.protectedPathsDesc",
  },
  {
    key: "dangerousBuiltins",
    labelKey: "toolSafety.alwaysOn.dangerousBuiltinsLabel",
    descKey: "toolSafety.alwaysOn.dangerousBuiltinsDesc",
  },
  {
    key: "mainProcessHook",
    labelKey: "toolSafety.alwaysOn.mainProcessHookLabel",
    descKey: "toolSafety.alwaysOn.mainProcessHookDesc",
  },
];

const saveStatus = ref<{ type: "success" | "error"; message: string } | null>(
  null,
);
let statusTimer: ReturnType<typeof setTimeout> | null = null;

function showStatus(type: "success" | "error", message: string): void {
  saveStatus.value = { type, message };
  if (statusTimer) clearTimeout(statusTimer);
  statusTimer = setTimeout(() => {
    saveStatus.value = null;
  }, 3500);
}

function syncDraft(): void {
  Object.assign(draft, config.value);
}

watch(config, syncDraft, { deep: true });

onMounted(async () => {
  await fetchConfig();
  syncDraft();
  await fetchDangerousPatterns();
});

/**
 * Persist a partial change. If the backend flagged it as reboot-requiring,
 * show the custom reboot-confirm dialog (decision 3B). On accept, drive the
 * shared reboot transition; on cancel, leave the saved-but-pending state with
 * a hint.
 */
async function persist(patch: Partial<RuntimeConfig>): Promise<void> {
  const { needsReboot } = await save(patch);
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
    showStatus("success", t("toolSafety.rebootDeferred"));
    return;
  }
  showStatus("success", t("toolSafety.saved"));
}

// ── Layer 1 ──
function toggleFileBroker(): void {
  draft.file_broker_enabled = !draft.file_broker_enabled;
  void persist({ file_broker_enabled: draft.file_broker_enabled });
}
function onMaxEntriesChange(value: number): void {
  if (!Number.isFinite(value) || value < 1) return;
  draft.file_broker_max_entries = value;
  void persist({ file_broker_max_entries: value });
}
function addSkipDir(): void {
  const dir = newSkipDir.value.trim();
  if (dir && !draft.project_skip_dirs.includes(dir)) {
    draft.project_skip_dirs = [...draft.project_skip_dirs, dir];
    newSkipDir.value = "";
    void persist({ project_skip_dirs: draft.project_skip_dirs });
  }
}
function removeSkipDir(dir: string): void {
  draft.project_skip_dirs = draft.project_skip_dirs.filter((d) => d !== dir);
  void persist({ project_skip_dirs: draft.project_skip_dirs });
}

// ── Layer 2 ──
// Single unified FileGuard switch: drives BOTH the Python tool-layer guard
// (`file_guard_enabled`) AND the OS-level subprocess hook
// (`native_file_guard_enabled`) together. The native hook is an implementation
// detail the user does not need to reason about. `file_guard_enabled` is the
// canonical "FileGuard on" indicator for display.
function toggleFileGuard(): void {
  const next = !draft.file_guard_enabled;
  draft.file_guard_enabled = next;
  draft.native_file_guard_enabled = next;
  void persist({
    file_guard_enabled: next,
    native_file_guard_enabled: next,
  });
}
function toggleAllowExec(): void {
  draft.allow_exec_tool = !draft.allow_exec_tool;
  void persist({ allow_exec_tool: draft.allow_exec_tool });
}

// ── Custom dangerous-command patterns (P-10) ──
// Add / remove edit only the union-only `extra` list; the built-in floor is
// read-only. Saving is a two-step: persist, then (since the patterns are baked
// at build time) prompt the reboot-confirm dialog so the change takes effect.
function addDangerousPatternFromInput(): void {
  const p = newDangerousPattern.value.trim();
  if (!p) return;
  addDangerousExtra(p);
  newDangerousPattern.value = "";
}

async function saveDangerousPatternsAndReboot(): Promise<void> {
  const { needsReboot, invalid } = await saveDangerousPatterns();
  if (invalid.length > 0) {
    showStatus(
      "error",
      t("toolSafety.dangerousCommands.invalidPatterns", {
        patterns: invalid.join(", "),
      }),
    );
    return;
  }
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
    showStatus("success", t("toolSafety.rebootDeferred"));
    return;
  }
  showStatus("success", t("toolSafety.saved"));
}

// ── Tool output limits (build-time → reboot, same as file_broker_max_entries) ──
function onReadMaxLinesChange(value: number): void {
  if (!Number.isFinite(value) || value < 1) return;
  draft.read_max_lines = value;
  void persist({ read_max_lines: value });
}
function onReadMaxBytesChange(value: number): void {
  if (!Number.isFinite(value) || value < 1024) return;
  draft.read_max_bytes = value;
  void persist({ read_max_bytes: value });
}
function onReadMaxLineLengthChange(value: number): void {
  if (!Number.isFinite(value) || value < 80) return;
  draft.read_max_line_length = value;
  void persist({ read_max_line_length: value });
}
function onGlobMaxResultsChange(value: number): void {
  if (!Number.isFinite(value) || value < 1) return;
  draft.glob_max_results = value;
  void persist({ glob_max_results: value });
}
function onGrepMaxMatchesChange(value: number): void {
  if (!Number.isFinite(value) || value < 1) return;
  draft.grep_max_matches = value;
  void persist({ grep_max_matches: value });
}
function onGrepMaxLineLengthChange(value: number): void {
  if (!Number.isFinite(value) || value < 80) return;
  draft.grep_max_line_length = value;
  void persist({ grep_max_line_length: value });
}
function onGrepMaxOutputBytesChange(value: number): void {
  if (!Number.isFinite(value) || value < 1024) return;
  draft.grep_max_output_bytes = value;
  void persist({ grep_max_output_bytes: value });
}
</script>

<template>
  <div class="tool-safety-panel">
    <header class="tool-safety-header">
      <h3>{{ t("toolSafety.title") }}</h3>
      <p class="tool-safety-subtitle">
        {{ t("toolSafety.subtitle") }}
      </p>
    </header>

    <!-- Layer 1 — Tool Guard (hot-applies) -->
    <section class="sec-section">
      <h4>{{ t("toolSafety.layer1Title") }}</h4>
      <p class="tool-safety-desc">
        {{ t("toolSafety.layer1Desc") }}
      </p>

      <div class="tool-safety-row">
        <div class="tool-safety-label">
          <span>{{ t("toolSafety.fileBrokerEnabled") }}</span>
          <small>{{ t("toolSafety.fileBrokerDesc") }}</small>
          <small class="tool-safety-hint">{{ t("toolSafety.fileBrokerRebootHint") }}</small>
        </div>
        <label class="sec-switch">
          <input
            type="checkbox"
            :checked="draft.file_broker_enabled"
            @change="toggleFileBroker"
          />
          <span class="sec-switch-slider"></span>
        </label>
      </div>

      <div class="tool-safety-row">
        <div class="tool-safety-label">
          <span>{{ t("toolSafety.maxEntries") }}</span>
        </div>
        <input
          class="tool-safety-number"
          type="number"
          min="1"
          :value="draft.file_broker_max_entries"
          @change="onMaxEntriesChange(Number(($event.target as HTMLInputElement).value))"
        />
      </div>

      <div class="tool-safety-row tool-safety-row--column">
        <div class="tool-safety-label">
          <span>{{ t("toolSafety.projectSkipDirs") }}</span>
          <small>{{ t("toolSafety.projectSkipDirsDesc") }}</small>
        </div>
        <div class="tool-safety-chips">
          <span
            v-for="dir in draft.project_skip_dirs"
            :key="dir"
            class="tool-safety-chip"
          >
            {{ dir }}
            <button
              type="button"
              :aria-label="t('toolSafety.remove')"
              @click="removeSkipDir(dir)"
            >
              ×
            </button>
          </span>
        </div>
        <div class="tool-safety-add">
          <input
            v-model="newSkipDir"
            class="tool-safety-text"
            type="text"
            :placeholder="t('toolSafety.projectSkipDirsPlaceholder')"
            @keyup.enter="addSkipDir"
          />
          <button
            type="button"
            class="tool-safety-add-btn"
            @click="addSkipDir"
          >
            {{ t("toolSafety.add") }}
          </button>
        </div>
      </div>
    </section>

    <!-- Layer 2 — Policy Guard -->
    <section class="sec-section">
      <h4>{{ t("toolSafety.layer2Title") }}</h4>
      <p class="tool-safety-desc">
        {{ t("toolSafety.layer2Desc") }}
      </p>

      <div class="tool-safety-row">
        <div class="tool-safety-label">
          <span>{{ t("toolSafety.fileGuardEnabled") }}</span>
          <small>{{ t("toolSafety.fileGuardDesc") }}</small>
        </div>
        <label class="sec-switch">
          <input
            type="checkbox"
            :checked="draft.file_guard_enabled"
            @change="toggleFileGuard"
          />
          <span class="sec-switch-slider"></span>
        </label>
      </div>

      <div class="tool-safety-row">
        <div class="tool-safety-label">
          <span>{{ t("toolSafety.allowExecTool") }}</span>
          <small>{{ t("toolSafety.allowExecToolDesc") }}</small>
        </div>
        <label class="sec-switch">
          <input
            type="checkbox"
            :checked="draft.allow_exec_tool"
            @change="toggleAllowExec"
          />
          <span class="sec-switch-slider"></span>
        </label>
      </div>
    </section>

    <!-- Custom dangerous-command patterns (P-10) — union-only override on top
       of the immutable built-in floor. The operator may only ADD extra regex
       patterns; the 9 built-in floor entries are shown greyed / read-only and
       CANNOT be removed (red line §9.2.4). Baked at build → reboot. -->
    <section class="sec-section">
      <h4>{{ t("toolSafety.dangerousCommands.title") }}</h4>
      <p class="tool-safety-desc">
        {{ t("toolSafety.dangerousCommands.desc") }}
      </p>

      <div class="tool-safety-alwayson-banner" role="note">
        🔒 {{ t("toolSafety.dangerousCommands.builtinBanner") }}
      </div>

      <div class="tool-safety-label">
        <span>{{ t("toolSafety.dangerousCommands.builtinLabel") }}</span>
      </div>
      <div class="tool-safety-chips">
        <span
          v-for="pat in dangerousBuiltin"
          :key="`builtin-${pat}`"
          class="tool-safety-chip tool-safety-chip--locked"
          :title="t('toolSafety.dangerousCommands.builtinLockedTitle')"
        >
          <code>{{ pat }}</code>
          <span class="tool-safety-chip-lock">🔒</span>
        </span>
      </div>

      <div class="tool-safety-row tool-safety-row--column">
        <div class="tool-safety-label">
          <span>{{ t("toolSafety.dangerousCommands.extraLabel") }}</span>
          <small>{{ t("toolSafety.dangerousCommands.extraDesc") }}</small>
          <small class="tool-safety-hint">{{ t("toolSafety.dangerousCommands.rebootHint") }}</small>
        </div>
        <div class="tool-safety-chips">
          <span
            v-for="pat in dangerousExtra"
            :key="`extra-${pat}`"
            class="tool-safety-chip"
          >
            <code>{{ pat }}</code>
            <button
              type="button"
              :aria-label="t('toolSafety.remove')"
              @click="removeDangerousExtra(pat)"
            >
              ×
            </button>
          </span>
        </div>
        <div class="tool-safety-add">
          <input
            v-model="newDangerousPattern"
            class="tool-safety-text"
            type="text"
            :placeholder="t('toolSafety.dangerousCommands.extraPlaceholder')"
            @keyup.enter="addDangerousPatternFromInput"
          />
          <button
            type="button"
            class="tool-safety-add-btn"
            @click="addDangerousPatternFromInput"
          >
            {{ t("toolSafety.add") }}
          </button>
        </div>
        <div class="tool-safety-add">
          <button
            type="button"
            class="tool-safety-add-btn"
            @click="saveDangerousPatternsAndReboot"
          >
            {{ t("toolSafety.dangerousCommands.save") }}
          </button>
        </div>
      </div>
    </section>

    <!-- Always-on security floors (3c switch-tree §6.4): the baseline
       protections that CANNOT be turned off by the operator — they do NOT
       read the security master switch and stay enforced under
       permissive/disabled. Rendered as read-only, greyed-out rows with an
       "always-on / immutable" badge (P-11 :disabled visual + a banner note),
       NOT as fake toggles. -->
    <section class="sec-section">
      <h4>{{ t("toolSafety.alwaysOn.title") }}</h4>
      <p class="tool-safety-desc">
        {{ t("toolSafety.alwaysOn.desc") }}
      </p>

      <div class="tool-safety-alwayson-banner" role="note">
        🔒 {{ t("toolSafety.alwaysOn.banner") }}
      </div>

      <div
        v-for="floor in alwaysOnFloors"
        :key="floor.key"
        class="tool-safety-row tool-safety-row--alwayson"
      >
        <div class="tool-safety-label">
          <span>{{ t(floor.labelKey) }}</span>
          <small>{{ t(floor.descKey) }}</small>
        </div>
        <div class="tool-safety-alwayson-control">
          <span class="tool-safety-alwayson-badge">{{ t("toolSafety.alwaysOn.badge") }}</span>
          <label
            class="sec-switch sec-switch--locked"
            :title="t('toolSafety.alwaysOn.lockedTitle')"
          >
            <input
              type="checkbox"
              checked
              disabled
              :aria-label="t(floor.labelKey)"
            />
            <span class="sec-switch-slider"></span>
          </label>
        </div>
      </div>
    </section>

    <!-- Tool Output Limits -->
    <section class="sec-section">
      <h4>{{ t("toolSafety.outputLimitsTitle") }}</h4>
      <p class="tool-safety-desc">
        {{ t("toolSafety.outputLimitsDesc") }}
      </p>

      <div class="tool-safety-row">
        <div class="tool-safety-label">
          <span>{{ t("toolSafety.readMaxLines") }}</span>
          <small>{{ t("toolSafety.readMaxLinesDesc") }}</small>
        </div>
        <input
          class="tool-safety-number"
          type="number"
          min="1"
          :value="draft.read_max_lines"
          @change="onReadMaxLinesChange(Number(($event.target as HTMLInputElement).value))"
        />
      </div>

      <div class="tool-safety-row">
        <div class="tool-safety-label">
          <span>{{ t("toolSafety.readMaxBytes") }}</span>
          <small>{{ t("toolSafety.readMaxBytesDesc") }}</small>
        </div>
        <input
          class="tool-safety-number"
          type="number"
          min="1024"
          :value="draft.read_max_bytes"
          @change="onReadMaxBytesChange(Number(($event.target as HTMLInputElement).value))"
        />
      </div>

      <div class="tool-safety-row">
        <div class="tool-safety-label">
          <span>{{ t("toolSafety.readMaxLineLength") }}</span>
          <small>{{ t("toolSafety.readMaxLineLengthDesc") }}</small>
        </div>
        <input
          class="tool-safety-number"
          type="number"
          min="80"
          :value="draft.read_max_line_length"
          @change="onReadMaxLineLengthChange(Number(($event.target as HTMLInputElement).value))"
        />
      </div>

      <div class="tool-safety-row">
        <div class="tool-safety-label">
          <span>{{ t("toolSafety.globMaxResults") }}</span>
          <small>{{ t("toolSafety.globMaxResultsDesc") }}</small>
        </div>
        <input
          class="tool-safety-number"
          type="number"
          min="1"
          :value="draft.glob_max_results"
          @change="onGlobMaxResultsChange(Number(($event.target as HTMLInputElement).value))"
        />
      </div>

      <div class="tool-safety-row">
        <div class="tool-safety-label">
          <span>{{ t("toolSafety.grepMaxMatches") }}</span>
          <small>{{ t("toolSafety.grepMaxMatchesDesc") }}</small>
        </div>
        <input
          class="tool-safety-number"
          type="number"
          min="1"
          :value="draft.grep_max_matches"
          @change="onGrepMaxMatchesChange(Number(($event.target as HTMLInputElement).value))"
        />
      </div>

      <div class="tool-safety-row">
        <div class="tool-safety-label">
          <span>{{ t("toolSafety.grepMaxLineLength") }}</span>
          <small>{{ t("toolSafety.grepMaxLineLengthDesc") }}</small>
        </div>
        <input
          class="tool-safety-number"
          type="number"
          min="80"
          :value="draft.grep_max_line_length"
          @change="onGrepMaxLineLengthChange(Number(($event.target as HTMLInputElement).value))"
        />
      </div>

      <div class="tool-safety-row">
        <div class="tool-safety-label">
          <span>{{ t("toolSafety.grepMaxOutputBytes") }}</span>
          <small>{{ t("toolSafety.grepMaxOutputBytesDesc") }}</small>
        </div>
        <input
          class="tool-safety-number"
          type="number"
          min="1024"
          :value="draft.grep_max_output_bytes"
          @change="onGrepMaxOutputBytesChange(Number(($event.target as HTMLInputElement).value))"
        />
      </div>
    </section>

    <div
      v-if="saveStatus"
      class="tool-safety-status"
      :class="`tool-safety-status--${saveStatus.type}`"
      role="status"
    >
      {{ saveStatus.message }}
    </div>
  </div>
</template>

<style scoped>
.tool-safety-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.tool-safety-header h3 {
  margin: 0 0 4px;
}
.tool-safety-subtitle,
.tool-safety-desc {
  margin: 0;
  color: var(--text-muted, #888);
  font-size: 13px;
}
.tool-safety-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
}
.tool-safety-row--column {
  flex-direction: column;
  align-items: stretch;
}
.tool-safety-label {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.tool-safety-label small {
  color: var(--text-muted, #888);
  font-size: 12px;
}
.tool-safety-hint {
  font-style: italic;
}
.tool-safety-number {
  width: 96px;
  background: var(--bg-input);
  color: var(--text-primary, #e0e0e0);
  border: 1px solid var(--border, rgba(127, 127, 127, 0.3));
  border-radius: var(--radius-sm, 4px);
  padding: var(--space-2, 6px) var(--space-3, 10px);
}
.tool-safety-number:focus {
  border-color: var(--accent, #58a6ff);
  outline: none;
  box-shadow: 0 0 0 3px var(--accent-muted, rgba(88, 166, 255, 0.15));
}
.tool-safety-text {
  width: 100%;
  box-sizing: border-box;
  background: var(--bg-input);
  color: var(--text-primary, #e0e0e0);
  border: 1px solid var(--border, rgba(127, 127, 127, 0.3));
  border-radius: var(--radius-sm, 4px);
  padding: var(--space-2, 6px) var(--space-3, 10px);
}
.tool-safety-text:focus {
  border-color: var(--accent, #58a6ff);
  outline: none;
  box-shadow: 0 0 0 3px var(--accent-muted, rgba(88, 166, 255, 0.15));
}
.tool-safety-text::placeholder {
  color: var(--text-muted, #888);
}
.tool-safety-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 6px 0;
}
.tool-safety-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 12px;
  background: var(--bg-tertiary);
  font-size: 12px;
}
.tool-safety-chip button {
  border: none;
  background: none;
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  color: var(--text-muted, #888);
}
.tool-safety-chip code {
  font-family: var(--font-mono, monospace);
  font-size: 11px;
}
.tool-safety-chip--locked {
  opacity: 0.7;
  cursor: not-allowed;
}
.tool-safety-chip-lock {
  font-size: 10px;
}
.tool-safety-add {
  display: flex;
  gap: 8px;
}
.tool-safety-add-btn {
  white-space: nowrap;
  background: var(--bg-tertiary);
  color: var(--text-primary, #e0e0e0);
  border: 1px solid var(--border, rgba(127, 127, 127, 0.3));
  border-radius: var(--radius-sm, 4px);
  padding: var(--space-2, 6px) var(--space-3, 10px);
  cursor: pointer;
  font-size: 13px;
  transition: background 0.15s, border-color 0.15s;
}
.tool-safety-add-btn:hover {
  background: var(--bg-hover);
  border-color: var(--accent, #58a6ff);
}
.tool-safety-status {
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 13px;
}
.tool-safety-status--success {
  background: rgba(46, 160, 67, 0.15);
  color: #2ea043;
}
.tool-safety-status--error {
  background: rgba(248, 81, 73, 0.15);
  color: #f85149;
}

/* ── Always-on security floors (3c §6.4) — read-only, locked rows ────── */
.tool-safety-alwayson-banner {
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 12px;
  background: rgba(88, 166, 255, 0.1);
  color: var(--text-secondary, #b0b0b0);
  border: 1px solid var(--border, rgba(127, 127, 127, 0.3));
  margin-bottom: 4px;
}
.tool-safety-row--alwayson {
  opacity: 0.75;
}
.tool-safety-alwayson-control {
  display: flex;
  align-items: center;
  gap: 10px;
}
.tool-safety-alwayson-badge {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  padding: 2px 8px;
  border-radius: 999px;
  white-space: nowrap;
  background: rgba(52, 211, 153, 0.15);
  color: var(--success, #34d399);
}
/* P-11 disabled visual: locked switch reads as inactive-but-on. */
.sec-switch--locked {
  cursor: not-allowed;
}
.sec-switch--locked input {
  cursor: not-allowed;
}
</style>
