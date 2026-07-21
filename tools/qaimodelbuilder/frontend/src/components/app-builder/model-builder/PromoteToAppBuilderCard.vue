<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * PromoteToAppBuilderCard — Model Builder → App Builder import UI.
 *
 * V1-parity rewrite (legacy
 * frontend/js/components/model-builder/PromoteToAppBuilderCard.js):
 *
 *   (a) Candidate branch — when dry-run surfaces importable plan items,
 *       offer a conflict policy + Validate / Import / Rollback, plus a
 *       provenance / validation warnings panel.
 *   (b) Workspace branch (core) — scan `<workdir>/output/*.bin` for
 *       precision variants, checkbox-select precisions, pick one default,
 *       then one-click Generate an App Builder Pack (auto-export).
 *
 * All logic lives in `usePromoteToAppBuilder`; this SFC is the render
 * layer only (keeps the file well under the cohesion budget).
 */
import { toRef, computed } from "vue";
import { useI18n } from "vue-i18n";
import { usePromoteToAppBuilder } from "@/composables/app-builder/usePromoteToAppBuilder";

interface Props {
  /** Absolute path to the session model working directory. */
  sessionModelWorkdir?: string;
}

const props = withDefaults(defineProps<Props>(), {
  sessionModelWorkdir: "",
});

const emit = defineEmits<{
  imported: [];
}>();

const { t, te } = useI18n();

const workdirRef = toRef(props, "sessionModelWorkdir");
const promote = usePromoteToAppBuilder(workdirRef, () => {
  emit("imported");
});

const {
  loading,
  error,
  success,
  hasWorkdir,
  planItems,
  showCommitCard,
  conflictPolicy,
  importing,
  lastCommitId,
  warnings,
  errors2,
  conflicts,
  suggestedVersion,
  validated,
  canImport,
  scanCandidates,
  commitImport,
  rollback,
  showVariantPickerStage,
  refresh,
  variants,
  needsNormalize,
  checkedPrecisions,
  defaultPrecision,
  scanLoading,
  showVariantPicker,
  canGenerate,
  exporting,
  togglePrecision,
  setDefaultPrecision,
  generatePack,
  fmtSize,
  fmtRelTime,
} = promote;

/** Resolve a warning code to its i18n message, falling back to the raw code. */
function warnText(code: string): string {
  const key = `modelBuilder.promote.warn.${code}`;
  return te(key) ? t(key) : code;
}

/** The sole detected variant in the single-variant branch (if any). */
const singleVariant = computed(() => variants.value[0]);

/**
 * "Why is Generate greyed out?" — a human-readable next-step hint.
 *
 * Returns `null` when the button is enabled (so the tooltip / hint line stays
 * out of the user's way). The priority mirrors `canGenerate` in the composable
 * (`usePromoteToAppBuilder.ts:196-201`) from most-specific to generic so the
 * message always tells the user the SINGLE next action to unblock — never a
 * vague "something is missing".
 *
 * Note: `hasWorkdir` is enforced by the outer `v-else` template guard (the
 * whole workspace branch, including this button, only renders when a workdir
 * is present), so it is structurally unreachable here and intentionally NOT
 * included as its own branch.
 */
const disabledReason = computed<string | null>(() => {
  if (canGenerate.value) return null;
  if (exporting.value) return t("modelBuilder.promote.disabledReason.exporting");
  if (variants.value.length === 0)
    return t("modelBuilder.promote.disabledReason.noBins");
  if (checkedPrecisions.value.length === 0)
    return t("modelBuilder.promote.disabledReason.noVariantSelected");
  if (defaultPrecision.value === "")
    return t("modelBuilder.promote.disabledReason.noDefaultVariant");
  return t("modelBuilder.promote.disabledReason.generic");
});
</script>

<template>
  <div class="promote-card">
    <header class="promote-card__header">
      <span
        class="promote-card__icon"
        aria-hidden="true"
      >&#x2b06;</span>
      <span class="promote-card__title">{{ t("modelBuilder.promote.title") }}</span>
      <button
        type="button"
        class="promote-card__refresh"
        :disabled="loading"
        :title="t('common.refresh')"
        @click="refresh"
      >
        &#x21bb;
      </button>
    </header>

    <div
      v-if="loading && !showCommitCard"
      class="promote-card__loading"
    >
      {{ t("common.loading") }}
    </div>
    <div
      v-if="error"
      class="promote-card__error"
    >
      {{ error }}
    </div>
    <div
      v-if="success"
      class="promote-card__success"
    >
      {{ success }}
    </div>

    <!-- (a) Candidate branch ─────────────────────────────────────────── -->
    <div
      v-if="showCommitCard"
      class="promote-card__candidate"
    >
      <!-- V1-parity rich candidate card: a SINGLE bordered card per candidate
           holding the title + meta + source + actions + dry-run result. (V2
           previously rendered the actions OUTSIDE this card, producing two
           stacked boxes — "两个对话框叠加".) -->
      <div
        v-for="item in planItems"
        :key="item.model_id"
        class="promote-card__item"
      >
        <div class="promote-card__item-header">
          <strong>{{ item.display_name || item.model_id }}</strong>
          <span class="promote-card__badge promote-card__badge--ready">
            {{ t("modelBuilder.promote.ready") }}
          </span>
        </div>
        <div class="promote-card__item-meta">
          <span>ID: {{ item.model_id }}</span>
          <span v-if="item.generated_at">{{ item.generated_at }}</span>
        </div>
        <div
          v-if="item.source"
          class="promote-card__item-source"
        >
          {{ item.source }}
        </div>

        <div class="promote-card__actions">
          <select
            v-model="conflictPolicy"
            class="promote-card__select"
          >
            <option value="bump">
              {{ t("modelBuilder.promote.policyBump") }}
            </option>
            <option value="replace">
              {{ t("modelBuilder.promote.policyReplace") }}
            </option>
            <option value="cancel">
              {{ t("modelBuilder.promote.policyCancel") }}
            </option>
          </select>
          <button
            type="button"
            class="promote-card__btn promote-card__btn--secondary"
            :disabled="loading"
            @click="scanCandidates"
          >
            {{ loading ? t("common.loading") : t("modelBuilder.promote.validate") }}
          </button>
          <button
            type="button"
            class="promote-card__btn promote-card__btn--primary"
            :disabled="importing || !canImport"
            @click="commitImport"
          >
            {{ importing ? t("common.loading") : t("modelBuilder.promote.import") }}
          </button>
          <button
            v-if="lastCommitId !== ''"
            type="button"
            class="promote-card__btn promote-card__btn--secondary"
            :disabled="importing"
            @click="rollback"
          >
            {{ t("modelBuilder.promote.rollback") }}
          </button>
          <!-- V2 enhancement: jump back to the pick-precision stage so the
               user can choose a different precision set and re-generate the
               Pack without first importing this (possibly stale) candidate. -->
          <button
            type="button"
            class="promote-card__btn promote-card__btn--link"
            :disabled="importing || exporting"
            data-testid="mb-promote-repick"
            @click="showVariantPickerStage"
          >
            {{ t("modelBuilder.promote.repickPrecision") }}
          </button>
        </div>

        <!-- Dry-run result (V1 parity: ✓ validation passed / ✗ errors /
             ⚠ conflicts / ⚠ warnings). Shown after Validate. State-Truth-
             First: the green pass line only renders when the candidate is
             REALLY importable AND has no version conflict (validated && no
             errors && no conflicts), never as a blanket "has candidate"
             success. A version-conflict is a decision the user must
             acknowledge (bump / replace / cancel) — showing "校验通过 — 可以
             导入" next to a "⚠ already exists" line was contradictory. -->
        <div
          v-if="validated || errors2.length > 0 || conflicts.length > 0 || warnings.length > 0"
          class="promote-card__dryrun"
        >
          <div
            v-if="validated && errors2.length === 0 && conflicts.length === 0 && warnings.length === 0"
            class="promote-card__dryrun--ok"
          >
            &#x2713; {{ t("modelBuilder.promote.validationPassed") }}
          </div>
          <div
            v-for="e in errors2"
            :key="'err-' + e"
            class="promote-card__dryrun--fail"
          >
            &#x2717; {{ e }}
          </div>
          <div
            v-for="cf in conflicts"
            :key="'conf-' + cf"
            class="promote-card__dryrun--conflict"
          >
            &#x26a0; {{ cf }}
          </div>
          <div
            v-if="suggestedVersion !== '' && conflicts.length > 0"
            class="promote-card__dryrun--conflict"
          >
            {{ t("modelBuilder.promote.suggestedVersion", { v: suggestedVersion }) }}
          </div>
          <div
            v-for="w in warnings"
            :key="w"
            class="promote-card__dryrun--warn"
          >
            &#x26a0; {{ warnText(w) }}
          </div>
        </div>
      </div>
    </div>

    <!-- (b) Workspace branch ─────────────────────────────────────────── -->
    <div
      v-else-if="!loading"
      class="promote-card__empty"
    >
      <template v-if="!hasWorkdir">
        {{ t("modelBuilder.promote.noWorkspace") }}
      </template>
      <template v-else>
        <div class="promote-card__workdir">
          {{ t("modelBuilder.promote.workspaceFound") }}
          <code>{{ props.sessionModelWorkdir }}</code>
        </div>

        <!-- Multi-variant picker (>=2 detected) -->
        <div
          v-if="showVariantPicker"
          class="promote-card__variants"
        >
          <div class="promote-card__variants-title">
            {{ t("modelBuilder.promote.scanBinsTitle") }}
          </div>
          <div
            v-for="b in variants"
            :key="b.precision"
            class="promote-card__variant-row"
          >
            <label class="promote-card__variant-check">
              <input
                type="checkbox"
                :checked="checkedPrecisions.includes(b.precision)"
                @change="togglePrecision(b.precision)"
              />
              <span class="promote-card__variant-label">{{ b.label }}</span>
            </label>
            <span class="promote-card__variant-size">{{ fmtSize(b.sizeBytes) }}</span>
            <span class="promote-card__variant-time">{{ fmtRelTime(b.mtime) }}</span>
          </div>

          <div class="promote-card__variants-default">
            <span class="promote-card__variants-default-label">
              {{ t("modelBuilder.promote.defaultPrecision") }}:
            </span>
            <label
              v-for="b in variants"
              :key="'def-' + b.precision"
              class="promote-card__variant-radio"
              :class="{ 'is-disabled': !checkedPrecisions.includes(b.precision) }"
            >
              <input
                type="radio"
                name="promote-default-precision"
                :value="b.precision"
                :checked="defaultPrecision === b.precision"
                :disabled="!checkedPrecisions.includes(b.precision)"
                @change="setDefaultPrecision(b.precision)"
              />
              <span>{{ b.label }}</span>
            </label>
          </div>
        </div>

        <!-- Single-variant: show what will be generated, no picker -->
        <div
          v-else-if="variants.length === 1"
          class="promote-card__variants"
        >
          <div class="promote-card__variants-title">
            {{ t("modelBuilder.promote.scanBinsTitle") }}
          </div>
          <div
            v-if="singleVariant"
            class="promote-card__variant-row"
          >
            <span class="promote-card__variant-label">{{ singleVariant.label }}</span>
            <span class="promote-card__variant-size">{{ fmtSize(singleVariant.sizeBytes) }}</span>
            <span class="promote-card__variant-time">{{ fmtRelTime(singleVariant.mtime) }}</span>
          </div>
        </div>

        <!-- Scanning in progress: the workdir is known but the output/ scan
             has not returned yet (e.g. the agent is still generating the model
             / variants). Without this, the variant list + needsNormalize +
             noBins branches are all suppressed (they gate on !scanLoading),
             leaving the popover visually EMPTY below the workdir line. Show a
             friendly "scanning…" line instead of a blank panel. -->
        <div
          v-else-if="scanLoading"
          class="promote-card__no-bins promote-card__scanning"
          data-testid="promote-scanning"
        >
          {{ t("modelBuilder.promote.scanning") }}
        </div>

        <!-- Un-normalized AI Hub model detected: guide to Step 6.5 instead of
             a bare "no bins" message. Fires when the scan found no output/
             variants BUT the workdir holds a downloaded-but-not-normalized AI
             Hub package (weight + metadata.json). -->
        <div
          v-else-if="!scanLoading && needsNormalize"
          class="promote-card__no-bins promote-card__needs-normalize"
          data-testid="promote-needs-normalize"
        >
          <div class="promote-card__needs-normalize-title">
            {{ t("modelBuilder.promote.needsNormalize.title") }}
          </div>
          <div class="promote-card__needs-normalize-body">
            {{ t("modelBuilder.promote.needsNormalize.body") }}
          </div>
          <code class="promote-card__needs-normalize-path">{{ needsNormalize.detected_weight }}</code>
        </div>

        <!-- No bins detected -->
        <div
          v-else-if="!scanLoading"
          class="promote-card__no-bins"
        >
          {{ t("modelBuilder.promote.noBinsHint") }}
        </div>

        <div class="promote-card__generate">
          <button
            type="button"
            class="promote-card__btn promote-card__btn--primary"
            :disabled="!canGenerate"
            :title="disabledReason ?? undefined"
            :aria-describedby="disabledReason ? 'promote-disabled-hint' : undefined"
            @click="generatePack"
          >
            {{ exporting ? t("modelBuilder.promote.generating") : t("modelBuilder.promote.generate") }}
          </button>
          <!--
            "Why is Generate greyed out?" hint (plan §6.4 A).

            Rendered as a persistent block (never v-if) with `min-height` so
            toggling between disabled/enabled does NOT push the surrounding
            content up/down — a layout jitter every time the user checks a
            precision. Empty content collapses to invisible via the ternary
            but the row height is reserved. Colour + size come from CSS vars
            so both light and dark themes stay readable (§14 UX rules).
          -->
          <p
            id="promote-disabled-hint"
            class="promote-card__disabled-hint"
            :aria-hidden="disabledReason ? undefined : 'true'"
          >
            {{ disabledReason ?? "" }}
          </p>
        </div>
      </template>
    </div>

    <slot />
  </div>
</template>

<style scoped>
.promote-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  /* V1 parity (`css/chat.css:2307` `.promote-card { padding: 12px }`): this
     card is the BODY of the `.rit-submenu` popover, which already paints the
     single container chrome (border + bg + radius + shadow). The card itself
     must NOT repaint its own border/background/radius, or the popover shows a
     double-layered box ("两层" — a card inside a card). So only padding +
     layout here; the submenu owns the surface. */
  padding: var(--space-3);
  color: var(--text-primary);
  font-size: var(--text-sm);
}

.promote-card__header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.promote-card__icon {
  font-size: var(--text-md);
}

.promote-card__title {
  flex: 1;
  font-size: var(--text-md);
  font-weight: 600;
}

.promote-card__refresh {
  border: 1px solid var(--border);
  border-radius: var(--radius-xs);
  background: var(--bg-tertiary);
  color: var(--text-secondary);
  cursor: pointer;
  padding: var(--space-1) var(--space-2);
  font-size: var(--text-base);
}

.promote-card__refresh:hover:not(:disabled) {
  background: var(--bg-hover);
}

.promote-card__loading {
  color: var(--text-muted);
}

.promote-card__error {
  color: var(--error);
  font-size: var(--text-sm);
}

.promote-card__success {
  color: var(--success);
  font-size: var(--text-sm);
}

.promote-card__candidate,
.promote-card__empty {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.promote-card__empty {
  color: var(--text-secondary);
}

.promote-card__list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.promote-card__list-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.promote-card__badge {
  font-size: var(--text-xs);
  padding: 0 var(--space-2);
  border-radius: var(--radius-full);
  background: var(--accent-muted);
  color: var(--accent);
}

/* V1-parity rich candidate card (icon + meta + source on its own line). */
.promote-card__item {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-xs);
  background: var(--bg-tertiary, var(--bg-secondary));
}
.promote-card__item-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-sm);
}
.promote-card__item-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
  font-size: var(--text-xs);
  color: var(--text-secondary, var(--text-muted));
}
.promote-card__item-source {
  font-size: var(--text-xs);
  color: var(--text-muted);
  font-family: var(--font-mono, monospace);
  word-break: break-all;
}
/* Green "Ready" status badge (V1 parity); the default __badge is the
   accent-tinted variant, this overrides it for the candidate card. */
.promote-card__badge--ready {
  background: var(--success-muted, var(--accent-muted));
  color: var(--success, var(--accent));
}

.promote-card__source {
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.promote-card__warn {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-2);
  border-radius: var(--radius-xs);
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
}

.promote-card__warn-row {
  color: var(--warning);
  font-size: var(--text-xs);
  line-height: 1.5;
}

/* Dry-run result lines (V1 parity), shown inside the candidate card below
   the actions. */
.promote-card__dryrun {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: var(--space-1);
  font-size: var(--text-xs);
  line-height: 1.5;
}
.promote-card__dryrun--ok {
  color: var(--success, #22c55e);
}
.promote-card__dryrun--fail {
  color: var(--error);
}
.promote-card__dryrun--conflict {
  color: var(--warning);
}
.promote-card__dryrun--warn {
  color: var(--warning);
}

.promote-card__actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-1);
}

.promote-card__select {
  padding: var(--space-1) var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-xs);
  background: var(--bg-input);
  color: var(--text-primary);
  font-size: var(--text-sm);
}

.promote-card__btn {
  padding: var(--space-1) var(--space-3);
  font-size: var(--text-sm);
  border: 1px solid var(--border);
  border-radius: var(--radius-xs);
  background: var(--bg-tertiary);
  color: var(--text-primary);
  cursor: pointer;
}

.promote-card__btn--secondary:hover:not(:disabled) {
  background: var(--bg-hover);
}

.promote-card__btn--primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}

.promote-card__btn--primary:hover:not(:disabled) {
  background: var(--accent-hover);
}

/* Text-only "re-pick precision" affordance (V2 enhancement) — visually a
   link, not a filled button, so it reads as a secondary escape hatch on the
   commit card without competing with the primary Import action. */
.promote-card__btn--link {
  background: transparent;
  border-color: transparent;
  color: var(--accent);
  padding-inline: var(--space-1);
  text-decoration: underline;
}

.promote-card__btn--link:hover:not(:disabled) {
  color: var(--accent-hover);
  background: transparent;
}

.promote-card__btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.promote-card__workdir {
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.promote-card__workdir code {
  font-family: var(--font-mono);
  color: var(--text-secondary);
}

.promote-card__variants {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-xs);
  background: var(--bg-tertiary);
}

.promote-card__variants-title {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: var(--space-1);
}

.promote-card__variant-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.promote-card__variant-check {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  flex: 1;
  cursor: pointer;
}

.promote-card__variant-label {
  font-weight: 500;
}

.promote-card__variant-size {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  min-width: 56px;
  text-align: right;
}

.promote-card__variant-time {
  font-size: var(--text-xs);
  color: var(--text-muted);
  min-width: 64px;
  text-align: right;
}

.promote-card__variants-default {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-2);
  padding-top: var(--space-2);
  border-top: 1px solid var(--border);
}

.promote-card__variants-default-label {
  font-size: var(--text-xs);
  color: var(--text-secondary);
}

.promote-card__variant-radio {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  font-size: var(--text-xs);
  cursor: pointer;
}

.promote-card__variant-radio.is-disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.promote-card__no-bins {
  font-size: var(--text-xs);
  color: var(--text-muted);
}

/* Un-normalized AI Hub model guidance — a slightly more prominent, actionable
   variant of the plain no-bins hint (accent left border + a code line showing
   the detected weight so the user can confirm the detection is real). */
.promote-card__needs-normalize {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-2);
  border-left: 3px solid var(--accent, #6d5efc);
  background: var(--bg-secondary, rgba(109, 94, 252, 0.06));
  border-radius: 4px;
}
.promote-card__needs-normalize-title {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--text-primary, inherit);
}
.promote-card__needs-normalize-body {
  font-size: var(--text-xs);
  color: var(--text-muted);
  line-height: 1.5;
}
.promote-card__needs-normalize-path {
  font-size: var(--text-xs);
  word-break: break-all;
  color: var(--text-muted);
  opacity: 0.85;
}

.promote-card__generate {
  margin-top: var(--space-1);
}

/*
 * "Why is Generate greyed out?" hint line (plan §6.4 A).
 *
 * Design constraints (§14 UX rules):
 *  - Colour + font-size come from CSS vars → light/dark themes both readable,
 *    same wavelength as the sibling `.promote-card__no-bins` hint.
 *  - `min-height` reserves the row even when the message is empty so toggling
 *    disabled ↔ enabled does NOT push the layout — no jitter on every click.
 *  - `margin-top: var(--space-1)` matches the vertical rhythm of the sibling
 *    hint blocks in this card (workdir / no-bins).
 */
.promote-card__disabled-hint {
  margin: var(--space-1) 0 0 0;
  min-height: 1.5em;
  font-size: var(--text-xs);
  color: var(--text-muted);
  line-height: 1.5;
}
</style>
