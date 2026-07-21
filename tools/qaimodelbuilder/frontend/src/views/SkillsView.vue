<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Skills view — V1-style card grid for skill registry management.
 * Uses `.skills-grid` / `.skill-card` global classes from settings.css.
 */
import { computed, onMounted, ref } from "vue";
import { storeToRefs } from "pinia";
import { useI18n } from "vue-i18n";
import { useSkillsStore, type Skill } from "@/stores/skills";
import { useHeaderActions } from "@/composables/useHeaderActions";
import { ICON_REFRESH } from "@/components/icons/topbarIcons";

const { t } = useI18n();
// Single shared source of truth (Pinia store) so toggling a skill here
// immediately updates the sidebar badge + composer "N skills active"
// indicator (they read the same store). The former `useSkills()` factory
// composable created an independent `skills` ref per call, so those two
// consumers never saw changes made on this page (V1 parity: a single
// `useSkills` instance drove all three).
const skillsStore = useSkillsStore();
const { skills, loading } = storeToRefs(skillsStore);
const { fetchSkills, reloadSkills, setSkillMode } = skillsStore;

// ─── Topbar actions (V1 parity: index.html — skills page header
// hosts 🔄 Reload button) ──────────────────────────────────────────────
useHeaderActions(() => [
  {
    id: "skills.reload",
    label: t("skills.reload"),
    iconSvg: ICON_REFRESH,
    title: t("skills.reload"),
    disabled: loading.value,
    onClick: () => {
      void reloadSkills();
    },
  },
]);

const searchQuery = ref("");
type FilterMode = "all" | "npu" | "enabled" | "disabled";
const activeFilter = ref<FilterMode>("all");

const stats = computed(() => {
  const total = skills.value.length;
  // effective mode: mode || (enabled ? cloud : off)
  const enabled = skills.value.filter(
    (s) => (s.mode ?? (s.enabled ? "cloud" : "off")) !== "off",
  ).length;
  const npu = skills.value.filter(
    (s) => s.mode === "local" || s.mode === "both",
  ).length;
  return { total, enabled, npu };
});

const statsLabel = computed(() => {
  const s = stats.value;
  return `${t("skills.discovered", { n: s.total })} · ${t("skills.enabled", { n: s.enabled })}`;
});

// V1 parity (SkillsPanel.js:26-28): NPU count rendered with var(--npu) color.
// Kept separate from statsLabel so the template can apply the color inline
// without resorting to v-html (XSS risk) or a computed that returns HTML.
const statsNpuLabel = computed(() => {
  const s = stats.value;
  if (s.npu <= 0) return "";
  return ` · ${t("skills.npuCount", { n: s.npu })}`;
});

const filteredSkills = computed(() => {
  let result = skills.value;

  // Apply filter
  if (activeFilter.value === "npu") {
    result = result.filter((s) => s.npu_optimized);
  } else if (activeFilter.value === "enabled") {
    result = result.filter((s) => s.enabled);
  } else if (activeFilter.value === "disabled") {
    result = result.filter((s) => !s.enabled);
  }

  // Apply search
  const q = searchQuery.value.trim().toLowerCase();
  if (q) {
    result = result.filter(
      (s) =>
        s.name.toLowerCase().includes(q) ||
        s.description.toLowerCase().includes(q) ||
        s.id.toLowerCase().includes(q) ||
        (s.use_for?.toLowerCase().includes(q) ?? false) ||
        (s.tags || []).some((tag) => tag.toLowerCase().includes(q)),
    );
  }

  return result;
});

/**
 * Emoji icon based on skill tags, falling back to ⚡ (V1 useSkills.js
 * skillEmoji parity). Tag-based mapping is more robust than per-id
 * hardcoding: new skills get a sensible icon from their tags automatically.
 */
function skillEmoji(skill: Skill): string {
  const tagMap: Record<string, string> = {
    email: "📧",
    web: "🌐",
    search: "🔍",
    file: "📁",
    code: "💻",
    data: "📊",
    image: "🖼️",
    audio: "🎵",
    video: "🎬",
    calendar: "📅",
    weather: "🌤️",
    finance: "💰",
    news: "📰",
    translate: "🌍",
  };
  for (const tag of skill.tags || []) {
    const hit = tagMap[tag.toLowerCase()];
    if (hit) return hit;
  }
  return "⚡";
}

/** True when the skill has a real icon URL (served by the backend). */
function hasIconUrl(skill: Skill): boolean {
  return !!skill.icon && /^\/?api\/|^https?:|^\//.test(skill.icon);
}

/** local / both modes require NPU optimization (V1 SkillManager rule). */
function modeDisabled(
  skill: Skill,
  mode: "off" | "cloud" | "local" | "both",
): boolean {
  if (mode === "local" || mode === "both") return !skill.npu_optimized;
  return false;
}

function handleSetMode(
  skillId: string,
  mode: "off" | "cloud" | "local" | "both",
): void {
  void setSkillMode(skillId, mode);
}

/**
 * Effective mode for a skill (V1 parity): explicit `mode`, else derived
 * from the legacy `enabled` boolean (`cloud` when on, `off` when off).
 */
function effectiveMode(skill: Skill): "off" | "cloud" | "local" | "both" {
  return (skill.mode ?? (skill.enabled ? "cloud" : "off")) as
    | "off"
    | "cloud"
    | "local"
    | "both";
}

/** Mode-specific status badge text (V1 statusOff/Cloud/Local/Both). */
function statusLabel(skill: Skill): string {
  const key = {
    off: "skills.statusOff",
    cloud: "skills.statusCloud",
    local: "skills.statusLocal",
    both: "skills.statusBoth",
  }[effectiveMode(skill)];
  return t(key);
}

onMounted(() => {
  void fetchSkills();
});
</script>

<template>
  <div class="panel-view">
    <!-- Header (title + stats; reload button moved to topbar via
         useHeaderActions for V1 parity — V1 hosts page-action buttons
         on the global topbar, not inside the body panel-header). -->
    <div class="panel-header">
      <div class="skills-header-left">
        <div class="skills-header-icon">
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            width="24"
            height="24"
          >
            <!-- V1 parity (SkillsPanel.js:18-21): lightning bolt / zap icon -->
            <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
          </svg>
          <span style="font-size: var(--text-lg); font-weight: 600">{{ t("skills.title") }}</span>
        </div>
        <div class="skills-stats">
          {{ statsLabel }}<span
            v-if="statsNpuLabel"
            style="color: var(--npu)"
          >{{ statsNpuLabel }}</span>
        </div>
      </div>
    </div>

    <!-- Filter bar -->
    <div class="skills-filter-bar">
      <input
        v-model="searchQuery"
        type="text"
        class="cloud-models-search"
        :placeholder="t('skills.search')"
      />
      <button
        type="button"
        class="btn btn-ghost btn-sm"
        :class="{ 'btn--active': activeFilter === 'all' }"
        @click="activeFilter = 'all'"
      >
        {{ t("skills.filterAll") }}
      </button>
      <button
        type="button"
        class="btn btn-ghost btn-sm"
        :class="{ 'btn--active': activeFilter === 'npu' }"
        @click="activeFilter = 'npu'"
      >
        ◆ {{ t("skills.filterNpu") }}
      </button>
      <button
        type="button"
        class="btn btn-ghost btn-sm"
        :class="{ 'btn--active': activeFilter === 'enabled' }"
        @click="activeFilter = 'enabled'"
      >
        {{ t("skills.filterEnabled") }}
      </button>
      <button
        type="button"
        class="btn btn-ghost btn-sm"
        :class="{ 'btn--active': activeFilter === 'disabled' }"
        @click="activeFilter = 'disabled'"
      >
        {{ t("skills.filterDisabled") }}
      </button>
    </div>

    <!-- Loading state (V1 parity: 6 skeleton cards in the grid) -->
    <div
      v-if="loading && skills.length === 0"
      class="skills-grid"
      style="padding: 16px 0"
    >
      <div
        v-for="n in 6"
        :key="`sk-skill-${n}`"
        class="skeleton-card"
      >
        <div class="skeleton-card-header">
          <div class="skeleton skeleton-circle skeleton-card-avatar" />
          <div class="skeleton-card-body">
            <div class="skeleton skeleton-line skeleton-line-medium" />
            <div class="skeleton skeleton-line skeleton-line-short" />
          </div>
        </div>
        <div class="skeleton skeleton-line skeleton-line-long" />
        <div
          class="skeleton skeleton-block"
          style="height: 32px; margin-top: 8px"
        />
      </div>
    </div>

    <!-- Empty state -->
    <div
      v-else-if="skills.length === 0"
      class="empty-state"
    >
      <div class="empty-state-icon">
        📂
      </div>
      <div class="empty-state-title">
        {{ t("skills.noSkills") }}
      </div>
      <p class="empty-state-text">
        {{ t("skills.noSkillsHint") }}
      </p>
    </div>

    <!-- No search results -->
    <div
      v-else-if="filteredSkills.length === 0"
      class="empty-state"
    >
      <p class="empty-state-text">
        {{ t("skills.noResults", { q: searchQuery }) }}
      </p>
    </div>

    <!-- Skills grid -->
    <div
      v-else
      class="skills-grid"
    >
      <div
        v-for="skill in filteredSkills"
        :key="skill.id"
        class="skill-card"
        :class="`skill-card-mode-${effectiveMode(skill)}`"
      >
        <!-- Header row: icon + title group + NPU badge -->
        <div class="skill-card-header-row">
          <div class="skill-icon">
            <img
              v-if="hasIconUrl(skill)"
              :src="skill.icon"
              :alt="skill.name"
              class="skill-icon-img"
            />
            <span v-else>{{ skillEmoji(skill) }}</span>
          </div>
          <div class="skill-card-title-group">
            <div class="skill-card-name">
              {{ skill.name }}
              <span
                v-if="skill.npu_optimized"
                class="npu-badge"
              >🔷 NPU</span>
            </div>
            <div class="skill-card-id">
              {{ skill.id }}
            </div>
          </div>
        </div>

        <!-- Description -->
        <div class="skill-card-desc">
          {{ skill.description }}
        </div>

        <!-- Use For -->
        <div
          v-if="skill.use_for"
          class="skill-card-use-for"
        >
          <strong>{{ t("skills.useFor") }}</strong> {{ skill.use_for }}
        </div>

        <!-- Tags -->
        <div
          v-if="skill.tags?.length"
          class="skill-tags"
        >
          <span
            v-for="tag in skill.tags"
            :key="tag"
            class="skill-tag"
          >{{ tag }}</span>
        </div>

        <!-- Separator -->
        <hr class="skill-card-divider" />

        <!-- Mode selector: 4 pill buttons (V1: off / cloud / local-NPU / both) -->
        <div class="skill-mode-selector">
          <button
            type="button"
            class="mode-btn mode-off"
            :class="{ active: effectiveMode(skill) === 'off' }"
            :title="t('skills.btnOffTitle')"
            @click="handleSetMode(skill.id, 'off')"
          >
            <span class="mode-btn-icon">✕</span>
            <span class="mode-btn-label">{{ t("skills.btnOff") }}</span>
          </button>
          <button
            type="button"
            class="mode-btn mode-cloud"
            :class="{ active: effectiveMode(skill) === 'cloud' }"
            :title="t('skills.btnCloudTitle')"
            @click="handleSetMode(skill.id, 'cloud')"
          >
            <span class="mode-btn-icon">☁</span>
            <span class="mode-btn-label">{{ t("skills.btnCloud") }}</span>
          </button>
          <button
            type="button"
            class="mode-btn mode-local"
            :class="{ active: effectiveMode(skill) === 'local', 'mode-btn-disabled': modeDisabled(skill, 'local') }"
            :title="modeDisabled(skill, 'local') ? t('skills.npuDisabledHint') : t('skills.btnLocalTitle')"
            @click="handleSetMode(skill.id, 'local')"
          >
            <span class="mode-btn-icon">⬡</span>
            <span class="mode-btn-label">{{ t("skills.btnLocal") }}</span>
          </button>
          <button
            type="button"
            class="mode-btn mode-both"
            :class="{ active: effectiveMode(skill) === 'both', 'mode-btn-disabled': modeDisabled(skill, 'both') }"
            :title="modeDisabled(skill, 'both') ? t('skills.npuDisabledHint') : t('skills.btnBothTitle')"
            @click="handleSetMode(skill.id, 'both')"
          >
            <span class="mode-btn-icon">⚡</span>
            <span class="mode-btn-label">{{ t("skills.btnBoth") }}</span>
          </button>
        </div>

        <!-- Footer: path + status (V1 SkillsPanel.js:120 — global
             .skill-card-actions; path carries both `skill-path`
             (max-width:160px so a long path can't squeeze the status into a
             1-char column) + `skill-card-path`; status pushed right via
             margin-left:auto) -->
        <div class="skill-card-actions">
          <span
            v-if="skill.skill_path"
            class="skill-path skill-card-path"
            :title="skill.skill_path"
          >
            {{ skill.skill_path }}
          </span>
          <span
            class="skill-card-status"
            :class="{ disabled: effectiveMode(skill) === 'off' }"
          >
            {{ statusLabel(skill) }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<style>
/* Skills-specific styles not covered by global settings.css.
   The skill card / mode-selector / npu-badge / tag / divider / path styles
   all live in styles/common/settings.css (full V1 parity) and are reused
   here — do not redefine them locally (avoids drift from the global tokens). */
.skills-header-left {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.skills-header-icon {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.skills-stats {
  font-size: var(--text-sm);
  color: var(--text-secondary);
}
.skills-filter-bar {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-4);
  flex-wrap: wrap;
}
.skills-filter-bar .cloud-models-search {
  flex: 1;
  min-width: 180px;
}
.btn--active {
  color: var(--accent) !important;
  border-color: var(--accent) !important;
  background: var(--accent-muted) !important;
}
.skill-icon-img {
  width: 28px;
  height: 28px;
  object-fit: contain;
  border-radius: var(--radius-sm);
}
</style>
