<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  LibraryBrowseList — the shared "built-in presets + mine" browse list used by
  all three template-library tabs (Agents / Teams / Modes).

  The three tabs render structurally-identical lists (a built-in section + a
  "mine" section, each row = name/meta + a row of action buttons). Extracted so
  that markup is written ONCE (细则 2 复用 > 重造) and TemplateLibraryDialog stays
  under the §3.6 1000-line soft cap. Purely presentational: the host supplies the
  two arrays + per-row content via the `item-main` and `actions` slots (each slot
  receives `{ entry, isBuiltin }`), and owns all CRUD/emit logic.

  Theme tokens only; the row visual language matches the host's .tl-* styles.
-->
<script setup lang="ts" generic="T extends { id: string }">
import { useI18n } from "vue-i18n";

defineProps<{
  builtins: T[];
  mine: T[];
  /** data-testid applied to each <li> (e.g. "library-agent-item"). */
  itemTestid?: string;
}>();

const { t } = useI18n();
</script>

<template>
  <section class="tl-section">
    <h3 class="tl-section-title">
      {{ t("chat.discussion.library.builtin") }}
    </h3>
    <ul class="tl-list">
      <li
        v-for="entry in builtins"
        :key="entry.id"
        class="tl-item"
        :data-testid="itemTestid"
      >
        <div class="tl-item-main">
          <slot name="item-main" :entry="entry" :is-builtin="true" />
        </div>
        <slot name="actions" :entry="entry" :is-builtin="true" />
      </li>
    </ul>
  </section>
  <section class="tl-section">
    <h3 class="tl-section-title">
      {{ t("chat.discussion.library.mine") }}
    </h3>
    <p v-if="mine.length === 0" class="tl-empty">
      {{ t("chat.discussion.library.empty") }}
    </p>
    <ul v-else class="tl-list">
      <li
        v-for="entry in mine"
        :key="entry.id"
        class="tl-item"
        :data-testid="itemTestid"
      >
        <div class="tl-item-main">
          <slot name="item-main" :entry="entry" :is-builtin="false" />
        </div>
        <slot name="actions" :entry="entry" :is-builtin="false" />
      </li>
    </ul>
  </section>
</template>

<style scoped>
.tl-section {
  margin-bottom: 16px;
}
.tl-section-title {
  margin: 8px 0;
  font-size: 0.78rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-secondary);
}
.tl-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.tl-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-tertiary);
}
.tl-item-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1 1 auto;
  min-width: 0;
}
.tl-empty {
  margin: 8px 0;
  font-size: 0.82rem;
  color: var(--text-secondary);
}
</style>
