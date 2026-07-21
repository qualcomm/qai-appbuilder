<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * AutoApprovePanel — V1-aligned auto-approve settings panel.
 *
 * Mirrors V1 `frontend/js/security/AutoApprovePanel.js` (209 lines).
 * Embedded as a sub-tab under SecurityView. Reuses the global shared
 * `.sec-section` / `.sec-list*` / `.sec-input` classes from
 * security-panels.css so the visual language matches the sibling security
 * panels (ProjectAccessPanel / ToolSafetyPanel).
 *
 * V1 sections (in order):
 *   1. Tool-level auto-approve toggles  — 5 ops (read/write/exec/glob/grep)
 *   2. Command whitelist                — enabled + prefix list (V1 default off)
 *   3. Command blacklist                — enabled + prefix list (V1 default ON)
 *   4. Read allow patterns              — enabled + glob pattern list
 *   5. Write allow patterns             — enabled + glob pattern list
 *   6. Actions                          — Reset Defaults + Save (right-aligned)
 *
 * State / API access via `useAutoApprove` composable
 * (see V1 `useAutoApprove.js`).
 */
import { ref, onMounted } from "vue";
import { useI18n } from "vue-i18n";

import { useAutoApprove } from "@/composables/useAutoApprove";

const { t } = useI18n();

const {
  loading,
  saving,
  autoApprove,
  commandWhitelist,
  commandBlacklist,
  readPatterns,
  writePatterns,
  loadSettings,
  saveAll,
  resetDefaults,
} = useAutoApprove();

// Local input state for "add new entry" rows.
const newWhitelistItem = ref("");
const newBlacklistItem = ref("");
const newReadPattern = ref("");
const newWritePattern = ref("");

const TOOL_OPS = ["read", "write", "exec", "glob", "grep"] as const;
type ToolOp = (typeof TOOL_OPS)[number];

onMounted(() => {
  void loadSettings();
});

// ─── Whitelist ───────────────────────────────────────────────────────────────
function addWhitelistItem(): void {
  const val = newWhitelistItem.value.trim();
  if (val && !commandWhitelist.prefixes.includes(val)) {
    commandWhitelist.prefixes.push(val);
  }
  newWhitelistItem.value = "";
}
function removeWhitelistItem(idx: number): void {
  commandWhitelist.prefixes.splice(idx, 1);
}

// ─── Blacklist ───────────────────────────────────────────────────────────────
function addBlacklistItem(): void {
  const val = newBlacklistItem.value.trim();
  if (val && !commandBlacklist.prefixes.includes(val)) {
    commandBlacklist.prefixes.push(val);
  }
  newBlacklistItem.value = "";
}
function removeBlacklistItem(idx: number): void {
  commandBlacklist.prefixes.splice(idx, 1);
}

// ─── Read patterns ───────────────────────────────────────────────────────────
function addReadPattern(): void {
  const val = newReadPattern.value.trim();
  if (val && !readPatterns.patterns.includes(val)) {
    readPatterns.patterns.push(val);
  }
  newReadPattern.value = "";
}
function removeReadPattern(idx: number): void {
  readPatterns.patterns.splice(idx, 1);
}

// ─── Write patterns ──────────────────────────────────────────────────────────
function addWritePattern(): void {
  const val = newWritePattern.value.trim();
  if (val && !writePatterns.patterns.includes(val)) {
    writePatterns.patterns.push(val);
  }
  newWritePattern.value = "";
}
function removeWritePattern(idx: number): void {
  writePatterns.patterns.splice(idx, 1);
}
</script>

<template>
  <div
    class="auto-approve-panel sec-config-panel"
    data-testid="auto-approve-panel"
  >
    <div
      v-if="loading"
      class="sec-list-empty"
    >
      {{ t("common.loading") }}
    </div>

    <template v-else>
      <!-- ═══ 1. Tool-level Auto-Approve Toggles ═══ -->
      <section class="sec-section">
        <h3 class="sec-section-title">
          {{ t("security.autoApprove.toolLevel") }}
        </h3>
        <p
          class="sec-field-desc"
          style="margin-bottom: var(--space-3)"
        >
          {{ t("security.autoApprove.toolLevelDesc") }}
        </p>
        <div class="auto-approve-toggles">
          <label
            v-for="op in TOOL_OPS"
            :key="op"
            class="sec-field-label auto-approve-toggle"
            :data-testid="`auto-approve-toggle-${op}`"
          >
            <input
              v-model="autoApprove[op as ToolOp]"
              type="checkbox"
            />
            <span>{{ t("security.autoApprove.op." + op) }}</span>
          </label>
        </div>
      </section>

      <!-- ═══ 2. Command Whitelist ═══ -->
      <section class="sec-section">
        <h3 class="sec-section-title">
          {{ t("security.autoApprove.whitelist") }}
        </h3>
        <label class="sec-field-label">
          <input
            v-model="commandWhitelist.enabled"
            type="checkbox"
            data-testid="auto-approve-whitelist-enabled"
          />
          <span>{{ t("common.enabled") }}</span>
        </label>
        <div
          v-if="commandWhitelist.enabled"
          class="sec-list"
          style="margin-top: var(--space-2)"
        >
          <div
            v-for="(item, idx) in commandWhitelist.prefixes"
            :key="`wl-${idx}`"
            class="auto-approve-list-item"
          >
            <code>{{ item }}</code>
            <button
              class="btn btn-ghost btn-sm auto-approve-list-remove"
              type="button"
              :title="t('common.remove')"
              :aria-label="`${t('common.remove')} ${item}`"
              @click="removeWhitelistItem(idx)"
            >
              &times;
            </button>
          </div>
          <div
            class="sec-list-row"
            style="margin-top: var(--space-1)"
          >
            <input
              v-model="newWhitelistItem"
              class="sec-input"
              :placeholder="t('security.autoApprove.whitelistPlaceholder')"
              data-testid="auto-approve-whitelist-input"
              @keyup.enter="addWhitelistItem"
            />
            <button
              class="btn btn-primary btn-sm"
              type="button"
              data-testid="auto-approve-whitelist-add"
              @click="addWhitelistItem"
            >
              {{ t('common.add') }}
            </button>
          </div>
        </div>
      </section>

      <!-- ═══ 3. Command Blacklist ═══ -->
      <section class="sec-section">
        <h3 class="sec-section-title">
          {{ t("security.autoApprove.blacklist") }}
        </h3>
        <label class="sec-field-label">
          <input
            v-model="commandBlacklist.enabled"
            type="checkbox"
            data-testid="auto-approve-blacklist-enabled"
          />
          <span>{{ t("common.enabled") }}</span>
        </label>
        <div
          v-if="commandBlacklist.enabled"
          class="sec-list"
          style="margin-top: var(--space-2)"
        >
          <div
            v-for="(item, idx) in commandBlacklist.prefixes"
            :key="`bl-${idx}`"
            class="auto-approve-list-item"
          >
            <code>{{ item }}</code>
            <button
              class="btn btn-ghost btn-sm auto-approve-list-remove"
              type="button"
              :title="t('common.remove')"
              :aria-label="`${t('common.remove')} ${item}`"
              @click="removeBlacklistItem(idx)"
            >
              &times;
            </button>
          </div>
          <div
            class="sec-list-row"
            style="margin-top: var(--space-1)"
          >
            <input
              v-model="newBlacklistItem"
              class="sec-input"
              :placeholder="t('security.autoApprove.blacklistPlaceholder')"
              data-testid="auto-approve-blacklist-input"
              @keyup.enter="addBlacklistItem"
            />
            <button
              class="btn btn-primary btn-sm"
              type="button"
              data-testid="auto-approve-blacklist-add"
              @click="addBlacklistItem"
            >
              {{ t('common.add') }}
            </button>
          </div>
        </div>
      </section>

      <!-- ═══ 4. Read Allow Patterns ═══ -->
      <section class="sec-section">
        <h3 class="sec-section-title">
          {{ t("security.autoApprove.readPatterns") }}
        </h3>
        <label class="sec-field-label">
          <input
            v-model="readPatterns.enabled"
            type="checkbox"
            data-testid="auto-approve-read-enabled"
          />
          <span>{{ t("common.enabled") }}</span>
        </label>
        <div
          v-if="readPatterns.enabled"
          class="sec-list"
          style="margin-top: var(--space-2)"
        >
          <div
            v-for="(item, idx) in readPatterns.patterns"
            :key="`rp-${idx}`"
            class="auto-approve-list-item"
          >
            <code>{{ item }}</code>
            <button
              class="btn btn-ghost btn-sm auto-approve-list-remove"
              type="button"
              :title="t('common.remove')"
              :aria-label="`${t('common.remove')} ${item}`"
              @click="removeReadPattern(idx)"
            >
              &times;
            </button>
          </div>
          <div
            class="sec-list-row"
            style="margin-top: var(--space-1)"
          >
            <input
              v-model="newReadPattern"
              class="sec-input"
              :placeholder="t('security.autoApprove.patternPlaceholder')"
              data-testid="auto-approve-read-input"
              @keyup.enter="addReadPattern"
            />
            <button
              class="btn btn-primary btn-sm"
              type="button"
              data-testid="auto-approve-read-add"
              @click="addReadPattern"
            >
              {{ t('common.add') }}
            </button>
          </div>
        </div>
      </section>

      <!-- ═══ 5. Write Allow Patterns ═══ -->
      <section class="sec-section">
        <h3 class="sec-section-title">
          {{ t("security.autoApprove.writePatterns") }}
        </h3>
        <label class="sec-field-label">
          <input
            v-model="writePatterns.enabled"
            type="checkbox"
            data-testid="auto-approve-write-enabled"
          />
          <span>{{ t("common.enabled") }}</span>
        </label>
        <div
          v-if="writePatterns.enabled"
          class="sec-list"
          style="margin-top: var(--space-2)"
        >
          <div
            v-for="(item, idx) in writePatterns.patterns"
            :key="`wp-${idx}`"
            class="auto-approve-list-item"
          >
            <code>{{ item }}</code>
            <button
              class="btn btn-ghost btn-sm auto-approve-list-remove"
              type="button"
              :title="t('common.remove')"
              :aria-label="`${t('common.remove')} ${item}`"
              @click="removeWritePattern(idx)"
            >
              &times;
            </button>
          </div>
          <div
            class="sec-list-row"
            style="margin-top: var(--space-1)"
          >
            <input
              v-model="newWritePattern"
              class="sec-input"
              :placeholder="t('security.autoApprove.patternPlaceholder')"
              data-testid="auto-approve-write-input"
              @keyup.enter="addWritePattern"
            />
            <button
              class="btn btn-primary btn-sm"
              type="button"
              data-testid="auto-approve-write-add"
              @click="addWritePattern"
            >
              {{ t('common.add') }}
            </button>
          </div>
        </div>
      </section>

      <!-- ═══ 6. Actions ═══ -->
      <div
        class="sec-actions sec-actions-row"
        style="justify-content: flex-end"
      >
        <button
          class="btn btn-ghost"
          type="button"
          data-testid="auto-approve-reset"
          @click="resetDefaults"
        >
          {{ t("security.autoApprove.resetDefaults") }}
        </button>
        <button
          class="btn btn-primary"
          type="button"
          data-testid="auto-approve-save"
          :disabled="saving"
          @click="saveAll"
        >
          {{ saving ? t("common.saving") : t("common.save") }}
        </button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.auto-approve-toggles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--space-2);
}

.auto-approve-toggle {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.auto-approve-list-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  padding: var(--space-1) var(--space-2);
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-1);
  margin-bottom: var(--space-1);
}

.auto-approve-list-item code {
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  color: var(--text-primary);
  flex: 1;
  overflow-wrap: anywhere;
}

.auto-approve-list-remove {
  flex-shrink: 0;
  font-size: var(--text-md);
  line-height: 1;
}
</style>
