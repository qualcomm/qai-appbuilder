<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ModelStrip — App Builder gallery model grid (V1 `ModelStrip.js` parity).
 *
 * Renders the cards for the active task selection as a grid of V1-parity
 * `ModelCard`s (full fields: name + variant-count badge + featured star +
 * category + IO + runtime + latency/mem + status dot). Click selects; the card
 * `info` event bubbles up so the workbench can open the info drawer.
 *
 * When the active selection holds more than {@link SEARCH_THRESHOLD} models a
 * fuzzy search box is shown (V1 `ModelStrip.js` parity: filter by displayName +
 * modelId + category + tags, with a clear button). Reuses the ready-made
 * `.ab-model-strip*` / `.ab-model-strip-search*` classes from the global
 * `styles/app-builder/app-builder.css` (real design tokens).
 */
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";
import ModelCard from "@/components/app-builder/ModelCard.vue";
import { useAppBuilderWorkbench } from "@/composables/app-builder/useAppBuilderWorkbench";
import type { AppModelCardVM } from "@/components/app-builder/types";

/** V1 parity: only surface the search box once the strip is busy enough. */
const SEARCH_THRESHOLD = 6;

interface Props {
  selectedId?: string | null;
}

const props = withDefaults(defineProps<Props>(), { selectedId: null });

const emit = defineEmits<{
  select: [id: string];
  info: [model: AppModelCardVM];
}>();

const { t } = useI18n();
const { cardsForSelection } = useAppBuilderWorkbench();

const allCards = computed<AppModelCardVM[]>(() => cardsForSelection.value);

const query = ref("");

const showSearch = computed<boolean>(() => allCards.value.length > SEARCH_THRESHOLD);

// V1 parity: fuzzy match across displayName + modelId + category + tags.
const cards = computed<AppModelCardVM[]>(() => {
  const list = allCards.value;
  const q = query.value.trim().toLowerCase();
  if (q === "") return list;
  return list.filter((c) => {
    const hay = [
      c.displayName,
      c.modelId,
      c.category,
      ...(Array.isArray(c.tags) ? c.tags : []),
    ]
      .filter((v): v is string => typeof v === "string" && v !== "")
      .join(" ")
      .toLowerCase();
    return hay.includes(q);
  });
});

function clearQuery(): void {
  query.value = "";
}
</script>

<template>
  <div
    class="ab-model-strip"
    role="listbox"
    :aria-label="t('appBuilder.aria.models')"
  >
    <div
      v-if="showSearch"
      class="ab-model-strip-search"
    >
      <input
        v-model="query"
        type="text"
        class="ab-model-strip-search-input"
        :placeholder="t('appBuilder.searchModels')"
        :aria-label="t('appBuilder.searchModels')"
      />
      <button
        v-if="query"
        type="button"
        class="ab-model-strip-search-clear"
        :aria-label="t('appBuilder.clear')"
        @click="clearQuery"
      >
        ×
      </button>
    </div>

    <div
      v-if="allCards.length === 0"
      class="ab-model-strip-empty"
    >
      {{ t("appBuilder.empty") }}
    </div>
    <div
      v-else
      class="ab-model-strip-row"
    >
      <ModelCard
        v-for="card in cards"
        :key="card.modelId"
        :model="card"
        :selected="card.modelId === props.selectedId"
        @select="emit('select', $event)"
        @info="emit('info', $event)"
      />
      <div
        v-if="cards.length === 0"
        class="ab-model-strip-empty"
      >
        {{ t("appBuilder.noMatches") }}
      </div>
    </div>
  </div>
</template>
