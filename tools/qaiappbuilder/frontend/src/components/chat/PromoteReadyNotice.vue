<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * PromoteReadyNotice — inline "your model is ready to promote" strip.
 *
 * Rendered by `ChatView` above the composer whenever
 * {@link usePromoteReadyDetection} says the active tab's model workspace has
 * scanned-eligible variants. Chosen as an INLINE notice (not a floating
 * toast) so:
 *   - the user controls when it disappears (dismiss); a 4.5s toast is easy
 *     to miss right after a long streaming turn where attention is drawn
 *     back to the message list;
 *   - the CTA sits in the natural reading path (above the composer, the
 *     place the user's cursor is heading anyway);
 *   - it participates in normal layout — no z-index games with the toast
 *     stack, no risk of hiding the Promote popover the user just clicked
 *     into (the popover is anchored to the toolbar and lives above this
 *     strip).
 *
 * Visual treatment
 * ----------------
 * Matches the `ModeIntroCard` restrained "helper strip" style: quiet
 * `--bg-secondary` background, a `--border` outline, no primary-color fill.
 * The CTA button uses the shared `.btn.btn-primary` class so it inherits the
 * accent color from the current theme. This deliberately does NOT compete
 * with the toolbar's Promote button (which the user will land on after the
 * click via `requestOpenPromote()`).
 *
 * Behaviour
 * ---------
 *   Primary CTA (→ Promote to App Builder)
 *     → `useModeFrameTriggers.requestOpenPromote()` bumps a shared token
 *       that the mode-frame components (`ModeFrameModelBuilder` /
 *       `ModeFrameAppBuilder`) already watch and use to open their local
 *       promote popover. Same wire the ModeIntroCard chip uses, so any
 *       future change to how the popover opens flows through one place.
 *     → After bumping, we dismiss the notice — the user has acted on it.
 *   Secondary "稍后" button
 *     → session-scoped dismiss for the current workdir (see composable
 *       docs for why this is not permanent).
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { useModeFrameTriggers } from "@/composables/useModeFrameTriggers";
import type { DetectedVariant } from "@/composables/usePromoteReadyDetection";

interface Props {
  /** Whether the notice should render (from the detection composable). */
  visible: boolean;
  /** Detected precision variants — drives the "{count} variants" copy. */
  variants: readonly DetectedVariant[];
  /** Detected model workspace (shown as a subtle path hint). */
  workdir: string;
}
const props = defineProps<Props>();

const emit = defineEmits<{
  /** User hit the "稍后" secondary button. */
  dismiss: [];
  /** User hit the primary "Promote" CTA. */
  promote: [];
}>();

const { t } = useI18n();
const { requestOpenPromote } = useModeFrameTriggers();

const variantCount = computed<number>(() => props.variants.length);

/**
 * Extract the model directory name from the workspace path for the tag
 * line ("`<model>` on <path>"). `workdir` is always
 * `<root>\<model>` (see `extractModelWorkdirFromMessages`), so the model
 * name is the last path segment.
 */
const modelName = computed<string>(() => {
  const wd = props.workdir;
  if (wd === "") return "";
  const parts = wd.split(/[\\/]+/).filter((p) => p.length > 0);
  return parts.length > 0 ? parts[parts.length - 1]! : "";
});

function onPromote(): void {
  // Route through the same shared "open the promote popover" trigger that
  // the ModeIntroCard chip uses — one wire, one behaviour. The mode-frame
  // whose watch is active (Model Builder OR App Builder) will pop open its
  // Promote card.
  requestOpenPromote();
  emit("promote");
}

function onDismiss(): void {
  emit("dismiss");
}
</script>

<template>
  <div
    v-if="visible"
    class="promote-ready-notice"
    role="status"
    aria-live="polite"
    data-testid="promote-ready-notice"
  >
    <span class="promote-ready-notice__icon" aria-hidden="true">🎉</span>
    <div class="promote-ready-notice__body">
      <p class="promote-ready-notice__title">
        {{ t("modelBuilder.promote.readyNotice.title") }}
      </p>
      <p class="promote-ready-notice__desc">
        {{
          t(
            variantCount === 1
              ? "modelBuilder.promote.readyNotice.descriptionOne"
              : "modelBuilder.promote.readyNotice.descriptionMany",
            { count: variantCount },
          )
        }}
        <span
          v-if="modelName !== ''"
          class="promote-ready-notice__model"
          :title="workdir"
        >· {{ modelName }}</span>
      </p>
    </div>
    <div class="promote-ready-notice__actions">
      <button
        type="button"
        class="promote-ready-notice__dismiss"
        data-testid="promote-ready-notice-dismiss"
        @click="onDismiss"
      >
        {{ t("modelBuilder.promote.readyNotice.dismiss") }}
      </button>
      <button
        type="button"
        class="promote-ready-notice__cta"
        data-testid="promote-ready-notice-cta"
        @click="onPromote"
      >
        {{ t("modelBuilder.promote.readyNotice.action") }}
      </button>
    </div>
  </div>
</template>

<style scoped>
/*
 * Visual language: mirrors ModeIntroCard's "helper strip" — quieter than
 * message bubbles / empty-state screens; never uses the accent color as a
 * fill (only the primary CTA button carries accent, via .promote-ready-
 * notice__cta below). All colours resolve to CSS tokens so light + dark
 * themes both look correct (AGENTS.md §3.10 / §5.3: NEVER hardcode a
 * discussion-theme colour).
 */
.promote-ready-notice {
  display: flex;
  align-items: center;
  gap: var(--space-3, 12px);
  margin: var(--space-2, 8px) var(--space-3, 12px);
  padding: 10px var(--space-3, 12px);
  background: var(--bg-secondary, #1c1c22);
  border: 1px solid var(--border, #2b2b33);
  border-radius: var(--radius-md, 10px);
  color: var(--text-primary, #e6e6e6);
}

.promote-ready-notice__icon {
  flex: 0 0 auto;
  font-size: 1.15rem;
  line-height: 1;
}

.promote-ready-notice__body {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.promote-ready-notice__title {
  margin: 0;
  font-size: 0.9rem;
  font-weight: 600;
  color: var(--text-primary, #e6e6e6);
}

.promote-ready-notice__desc {
  margin: 0;
  font-size: 0.82rem;
  line-height: 1.4;
  color: var(--text-secondary, #a0a0a8);
  /* Long workdir tails shouldn't overflow the strip; ellipsize the whole
     description line — the full path lives in the model-name span's title
     attribute so users can still see it on hover. */
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.promote-ready-notice__model {
  color: var(--text-secondary, #a0a0a8);
}

.promote-ready-notice__actions {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

/* Ghost "稍后" button — matches ModeIntroCard's close-x visual weight so it
   reads as an unobtrusive secondary action. */
.promote-ready-notice__dismiss {
  padding: 5px 10px;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm, 6px);
  color: var(--text-secondary, #a0a0a8);
  font-size: 0.82rem;
  cursor: pointer;
  transition: background 0.12s ease, color 0.12s ease;
}
.promote-ready-notice__dismiss:hover {
  background: var(--bg-hover, rgba(255, 255, 255, 0.06));
  color: var(--text-primary, #e6e6e6);
}

/* Primary CTA — accent fill, matches the shared .btn.btn-primary weight so
   it visually reads as "the action to take" without duplicating that class
   name (kept scoped-local to avoid style leaks into other .btn instances). */
.promote-ready-notice__cta {
  padding: 6px 14px;
  background: var(--accent, #6d5efc);
  border: 1px solid var(--accent, #6d5efc);
  border-radius: var(--radius-sm, 6px);
  color: var(--text-on-accent, #ffffff);
  font-size: 0.85rem;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.12s ease, border-color 0.12s ease;
}
.promote-ready-notice__cta:hover {
  background: var(--accent-hover, #7d70ff);
  border-color: var(--accent-hover, #7d70ff);
}

/* Narrow layout: on a compressed side pane the model-name suffix wraps
   under the title; the actions stay on the right. */
@media (max-width: 520px) {
  .promote-ready-notice {
    flex-wrap: wrap;
  }
  .promote-ready-notice__body {
    flex-basis: 100%;
  }
  .promote-ready-notice__actions {
    margin-left: auto;
  }
}
</style>
