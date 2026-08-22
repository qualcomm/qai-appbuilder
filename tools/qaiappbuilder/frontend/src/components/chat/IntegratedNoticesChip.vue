<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * IntegratedNoticesChip — N.53.
 *
 * A compact "📥 integrated N background notices" chip rendered ABOVE an
 * assistant answer that folded background SYSTEM_NOTICE rows into its own
 * turn (Batch 7's turn-internal integration).
 *
 * Why it exists: SYSTEM_NOTICE rows are wire-fold-only — they are folded into
 * the provider wire as `[System notification] …` and deliberately NEVER
 * rendered as bubbles (see `historyMapper._mapSystemNoticeRow`; rendering them
 * produced the "wall of italic grey" artefact users complained about). The
 * consequence was that an answer which digested three finished sub-agents
 * looked identical to one that ignored them — the integration was invisible.
 *
 * This chip is the minimum visible trace: it states HOW MANY notices the
 * answer folded in, and expands to show each notice's full text so the user
 * can check what the model was told. Collapsed by default so the noise the
 * fold-only decision avoided does not come back.
 *
 * The notice→answer pairing is resolved in the mapper (`integratedNotices`),
 * not here, so this component is a pure presenter of an already-joined list.
 */
import { ref } from "vue";
import { useI18n } from "vue-i18n";
import type { IntegratedNotice } from "@/stores/chatTabs";

const props = defineProps<{
  /** Notices this answer integrated, already paired with their text by
   *  `mapHistoryItems`. Never empty — the parent renders this component only
   *  when the message actually carries notices. */
  notices: IntegratedNotice[];
}>();

const { t } = useI18n();

/** Collapsed by default: the count is the signal, the texts are the detail. */
const expanded = ref(false);
</script>

<template>
  <div class="integrated-notices" data-testid="integrated-notices">
    <button
      type="button"
      class="integrated-notices__chip"
      :aria-expanded="expanded"
      data-testid="integrated-notices-chip"
      @click="expanded = !expanded"
    >
      <span aria-hidden="true">📥</span>
      <span>{{ t("chat.integratedNoticesLabel", { n: props.notices.length }) }}</span>
      <span
        class="integrated-notices__caret"
        :class="{ 'integrated-notices__caret--open': expanded }"
        aria-hidden="true"
      >›</span>
    </button>
    <ul v-if="expanded" class="integrated-notices__list">
      <li
        v-for="notice in props.notices"
        :key="notice.dedupKey"
        class="integrated-notices__item"
      >
        <!-- Empty text = the notice row sits on an older history page than
             this answer (pagination). The count above still includes it, so
             show the placeholder rather than a blank row. -->
        {{ notice.text || t("chat.integratedNoticeUnavailable") }}
      </li>
    </ul>
  </div>
</template>

<style scoped>
.integrated-notices {
  margin-bottom: var(--space-1);
}

/* Chip reads as a quiet affordance, not an alert: this is provenance
   information, and it must never compete with the answer it annotates. */
.integrated-notices__chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px var(--space-2);
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg-tertiary);
  color: var(--text-secondary);
  font-size: 0.8em;
  cursor: pointer;
  transition: background var(--transition), border-color var(--transition);
}
.integrated-notices__chip:hover {
  background: var(--bg-hover);
  border-color: var(--accent);
  color: var(--text-primary);
}
.integrated-notices__chip:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.integrated-notices__caret {
  display: inline-block;
  transition: transform 0.15s;
}
.integrated-notices__caret--open {
  transform: rotate(90deg);
}

.integrated-notices__list {
  margin: var(--space-1) 0 0;
  padding: var(--space-1) var(--space-2);
  border-left: 2px solid var(--border);
  list-style: none;
  color: var(--text-secondary);
  font-size: 0.85em;
}
.integrated-notices__item {
  padding: 2px 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
