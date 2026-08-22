<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ExecProfilesTable — loaded command-execution profiles (read-only).
 *
 * Renders what the exec-profile broker ACTUALLY loaded from
 * `factory/config/exec_profiles/*.toml`, i.e. the content the
 * `command_policy_enabled` switch above it turns on. Lives inside
 * Security > Tool Safety layer 3 ("command execution") because a profile
 * gates *commands*; it previously sat at the bottom of the Skill panel,
 * the wrong semantic domain.
 *
 * Each row summarises the classification config that actually decides a
 * command's fate (`CommandProfile.classify`):
 *   - ASK — `ask_args` + `ask_rules` (subcommand-aware) → permission popup
 *   - DENY — `hard_deny_args` + legacy `denied_args` → hard block
 *   - "always ask" — `ask_always`: any invocation prompts
 *   - IO — `io_constraints`: input/output directory limits
 * The legacy `allowed_args` / `denied_args` pair that the old table showed
 * exclusively is empty in every shipped profile, which is why every row
 * rendered as "—".
 *
 * Read-only by design: profiles are static compiled assets with no write
 * endpoint. The reload button re-reads them from disk.
 */
import { onMounted } from "vue";
import { useI18n } from "vue-i18n";

import { useExecProfiles } from "@/composables/useExecProfiles";

const { t } = useI18n();

const {
  loading,
  reloading,
  profiles,
  expanded,
  profileCount,
  matchPatterns,
  askTokens,
  denyTokens,
  ioConstraintRows,
  isPermissive,
  toggleExpanded,
  fetchProfiles,
  reloadProfiles,
} = useExecProfiles();

onMounted(() => {
  void fetchProfiles();
});
</script>

<template>
  <div class="exec-profiles">
    <div class="exec-profiles-head">
      <div class="tool-safety-label">
        <span>{{ t("execBroker.profiles") }} ({{ profileCount }})</span>
        <small>{{ t("execBroker.profilesDesc") }}</small>
      </div>
      <button
        type="button"
        class="btn btn-ghost btn-sm"
        :disabled="loading || reloading"
        @click="reloadProfiles"
      >
        ↺ {{ t("execBroker.reload") }}
      </button>
    </div>

    <div
      v-if="loading && !profiles.length"
      class="exec-profiles-empty"
    >
      {{ t("common.loading") }}
    </div>
    <div
      v-else-if="!profiles.length"
      class="exec-profiles-empty"
    >
      {{ t("execBroker.noProfiles") }}
    </div>

    <ul
      v-else
      class="exec-profiles-list"
    >
      <li
        v-for="prof in profiles"
        :key="prof.name"
        class="exec-profile"
      >
        <button
          type="button"
          class="exec-profile-row"
          :aria-expanded="expanded === prof.name"
          @click="toggleExpanded(prof.name)"
        >
          <span class="exec-profile-caret">
            {{ expanded === prof.name ? "▾" : "▸" }}
          </span>
          <code class="exec-profile-name">{{ prof.name }}</code>
          <span class="exec-profile-badges">
            <span
              v-if="prof.ask_always"
              class="exec-badge exec-badge--ask"
            >
              {{ t("execBroker.badgeAskAlways") }}
            </span>
            <span
              v-if="askTokens(prof).length"
              class="exec-badge exec-badge--ask"
            >
              {{ t("execBroker.badgeAsk", { n: askTokens(prof).length }) }}
            </span>
            <span
              v-if="denyTokens(prof).length"
              class="exec-badge exec-badge--deny"
            >
              {{ t("execBroker.badgeDeny", { n: denyTokens(prof).length }) }}
            </span>
            <span
              v-if="ioConstraintRows(prof).length"
              class="exec-badge exec-badge--io"
            >
              {{ t("execBroker.badgeIo") }}
            </span>
            <span
              v-if="isPermissive(prof)"
              class="exec-badge exec-badge--none"
            >
              {{ t("execBroker.badgeNoRules") }}
            </span>
          </span>
          <span class="exec-profile-match">
            {{ matchPatterns(prof).join(", ") || "—" }}
          </span>
        </button>

        <div
          v-if="expanded === prof.name"
          class="exec-profile-detail"
        >
          <p
            v-if="prof.description"
            class="exec-profile-desc"
          >
            {{ prof.description }}
          </p>

          <div
            v-if="prof.ask_always"
            class="exec-profile-note"
          >
            {{ t("execBroker.askAlwaysDesc") }}
          </div>

          <div
            v-if="denyTokens(prof).length"
            class="exec-profile-field"
          >
            <span class="exec-profile-field-label">
              {{ t("execBroker.colDeniedPatterns") }}
            </span>
            <span class="exec-profile-chips">
              <code
                v-for="tok in denyTokens(prof)"
                :key="`d-${tok}`"
                class="exec-chip exec-chip--deny"
              >{{ tok }}</code>
            </span>
          </div>

          <div
            v-if="prof.ask_args.length"
            class="exec-profile-field"
          >
            <span class="exec-profile-field-label">
              {{ t("execBroker.colAskArgs") }}
            </span>
            <span class="exec-profile-chips">
              <code
                v-for="tok in prof.ask_args"
                :key="`a-${tok}`"
                class="exec-chip exec-chip--ask"
              >{{ tok }}</code>
            </span>
          </div>

          <div
            v-if="prof.ask_rules.length"
            class="exec-profile-field"
          >
            <span class="exec-profile-field-label">
              {{ t("execBroker.colAskRules") }}
            </span>
            <ul class="exec-profile-rules">
              <li
                v-for="(rule, i) in prof.ask_rules"
                :key="`r-${i}`"
              >
                <code class="exec-chip exec-chip--ask">
                  {{ prof.name }} {{ rule.subcommand }}
                  {{ [...(rule.any_flags ?? []), ...(rule.positional_any ?? [])].join(" | ") }}
                </code>
                <small v-if="rule.reason">{{ rule.reason }}</small>
              </li>
            </ul>
          </div>

          <div
            v-if="prof.allowed_args.length"
            class="exec-profile-field"
          >
            <span class="exec-profile-field-label">
              {{ t("execBroker.colAllowedCommands") }}
            </span>
            <span class="exec-profile-chips">
              <code
                v-for="tok in prof.allowed_args"
                :key="`w-${tok}`"
                class="exec-chip"
              >{{ tok }}</code>
            </span>
          </div>

          <div
            v-if="ioConstraintRows(prof).length"
            class="exec-profile-field"
          >
            <span class="exec-profile-field-label">
              {{ t("execBroker.colIoConstraints") }}
            </span>
            <span class="exec-profile-chips">
              <code
                v-for="row in ioConstraintRows(prof)"
                :key="`io-${row}`"
                class="exec-chip"
              >{{ row }}</code>
            </span>
          </div>

          <div
            v-if="prof.source_skill"
            class="exec-profile-field"
          >
            <span class="exec-profile-field-label">
              {{ t("execBroker.colSourceSkill") }}
            </span>
            <code class="exec-chip">{{ prof.source_skill }}</code>
          </div>

          <div
            v-if="isPermissive(prof)"
            class="exec-profile-note"
          >
            {{ t("execBroker.noRulesDesc") }}
          </div>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.exec-profiles {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  width: 100%;
}

.exec-profiles-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}

.exec-profiles-empty {
  padding: var(--space-3);
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.exec-profiles-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin: 0;
  padding: 0;
  list-style: none;
}

.exec-profile {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  overflow: hidden;
}

.exec-profile-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  padding: var(--space-2) var(--space-3);
  background: transparent;
  border: 0;
  cursor: pointer;
  text-align: left;
  font: inherit;
  color: inherit;
}

.exec-profile-row:hover {
  background: var(--bg-subtle);
}

.exec-profile-caret {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.exec-profile-name {
  font-weight: 600;
  min-width: 8rem;
}

.exec-profile-badges {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
}

.exec-profile-match {
  margin-left: auto;
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 40%;
}

.exec-badge {
  padding: 0 var(--space-2);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  line-height: 1.6;
  background: var(--bg-subtle);
  color: var(--text-muted);
}

.exec-badge--ask {
  background: var(--warning-bg, #fff4e5);
  color: var(--warning-text, #8a5300);
}

.exec-badge--deny {
  background: var(--danger-bg, #fdecec);
  color: var(--danger-text, #a11);
}

.exec-badge--io {
  background: var(--info-bg, #eaf2fd);
  color: var(--info-text, #24507e);
}

.exec-profile-detail {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3) var(--space-3);
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-subtle);
}

.exec-profile-desc,
.exec-profile-note {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--text-xs);
  line-height: 1.6;
}

.exec-profile-field {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: baseline;
}

.exec-profile-field-label {
  min-width: 7rem;
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.exec-profile-chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
  flex: 1 1 60%;
}

.exec-chip {
  padding: 0 var(--space-1);
  border-radius: var(--radius-sm);
  background: var(--bg-base);
  border: 1px solid var(--border-subtle);
  font-size: var(--text-xs);
}

.exec-chip--ask {
  border-color: var(--warning-text, #8a5300);
}

.exec-chip--deny {
  border-color: var(--danger-text, #a11);
}

.exec-profile-rules {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin: 0;
  padding: 0;
  list-style: none;
  flex: 1 1 60%;
}

.exec-profile-rules small {
  display: block;
  color: var(--text-muted);
  font-size: var(--text-xs);
  line-height: 1.5;
}
</style>
