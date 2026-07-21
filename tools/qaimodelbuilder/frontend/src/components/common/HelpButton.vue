<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * HelpButton — reusable "ℹ️" help trigger for settings / complex forms.
 *
 * Design goals (see Plan §1.4, decision 6):
 *   - One shared component wired at multiple hook-in points (MCP servers,
 *     cloud models, cloud-model API key onboarding, download settings,
 *     WeChat/iLink) so every settings panel has an actionable help entry.
 *   - Content is authored as Markdown files under
 *     `frontend/src/help-content/<docKey>.<locale>.md` and loaded eagerly at
 *     build time via `import.meta.glob(..., { as: 'raw' })`. This keeps the
 *     runtime deps to zero (marked + DOMPurify are already project deps for
 *     chat rendering, reused here through `renderMarkdown`), and unresolved
 *     doc keys are a build-time detectable typo rather than a runtime 404.
 *   - Modal chrome is the same `ChannelInfoDialog` used by WeChat / Feishu
 *     info dialogs (Teleport + focus-trap + Esc/backdrop/×/footer-close).
 *     One source of truth for dialog chrome; consistent behaviour across
 *     features; no new "yet another modal shell" reinvented here.
 *
 * The Markdown body is trusted (authored in-repo, not user input) and is
 * rendered via `renderMarkdown` → marked → DOMPurify, which also strips any
 * unsafe attributes; we additionally rewrite every `<a>` post-parse to
 * carry `target="_blank"` + `rel="noopener noreferrer"` so help links can
 * never trigger tab-nap on the settings panel.
 */
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";

import ChannelInfoDialog from "@/components/channels/ChannelInfoDialog.vue";
import { renderMarkdown } from "@/composables/markdown";

interface Props {
  /** Key that resolves the markdown file (`{docKey}.{locale}.md`). */
  docKey: string;
  /** Optional official documentation URL rendered as a footer button. */
  externalUrl?: string;
  /** Optional aria-label override; falls back to `common.help.button.ariaLabel`. */
  ariaLabel?: string;
  /**
   * `sm` = 16px icon (in-panel headers, next to titles); `md` = 20px icon
   * (standalone). Both keep a 32×32 hit area (WCAG 2.5.5 target size).
   */
  size?: "sm" | "md";
  /** Optional modal title override; falls back to `common.help.title`. */
  title?: string;
}

const props = withDefaults(defineProps<Props>(), {
  size: "sm",
  externalUrl: "",
  ariaLabel: "",
  title: "",
});

const { t, locale } = useI18n();

const open = ref<boolean>(false);
function show(): void {
  open.value = true;
}
function hide(): void {
  open.value = false;
}

// Eager-load every help-content markdown at build time. `as: 'raw'` inlines
// the file contents as strings so we can pick the right one purely by key.
// (Vite `import.meta.glob` is the project-standard static import mechanism.)
const rawMdModules = import.meta.glob<string>(
  "../../help-content/*.md",
  { eager: true, query: "?raw", import: "default" },
);

/** Strip the leading "../../help-content/" and trailing ".md" from a glob key. */
function baseName(globKey: string): string {
  return globKey.replace(/^.*\/help-content\//, "").replace(/\.md$/, "");
}

/**
 * Pick the best-fit markdown source for `docKey` at the current locale.
 * Fallback chain: current locale → `en` → any (first alphabetically) → `""`.
 * Returning `""` triggers the "load failed" branch in the template.
 */
const mdSource = computed<string>(() => {
  const key = props.docKey;
  const loc = locale.value;
  const bestKey = `${key}.${loc}`;
  const fallbackKey = `${key}.en`;
  // Fast-path lookups
  for (const [globKey, raw] of Object.entries(rawMdModules)) {
    if (baseName(globKey) === bestKey) return raw;
  }
  for (const [globKey, raw] of Object.entries(rawMdModules)) {
    if (baseName(globKey) === fallbackKey) return raw;
  }
  // Last-resort: any localised variant of this doc key
  for (const [globKey, raw] of Object.entries(rawMdModules)) {
    const bn = baseName(globKey);
    if (bn.startsWith(`${key}.`)) return raw;
  }
  return "";
});

/**
 * Render the markdown → sanitised HTML, then rewrite anchor tags so every
 * external link opens in a new tab and cannot access `window.opener`.
 * We do this post-DOMPurify: `renderMarkdown` already sanitises the tree,
 * so a regex-based `target=/rel=` inject on the string is safe and cheap.
 */
const renderedHtml = computed<string>(() => {
  const source = mdSource.value;
  if (!source) return "";
  const html = renderMarkdown(source, { markedOptions: { breaks: true } });
  // Force target=_blank + rel=noopener on every <a> (idempotent replacement).
  return html.replace(/<a\b([^>]*?)>/gi, (match, attrs: string) => {
    let cleaned = attrs;
    // Drop any existing target / rel so ours wins deterministically.
    cleaned = cleaned.replace(/\s+target=(["'])[^"']*\1/gi, "");
    cleaned = cleaned.replace(/\s+rel=(["'])[^"']*\1/gi, "");
    return `<a${cleaned} target="_blank" rel="noopener noreferrer">`;
  });
});

const hasContent = computed<boolean>(() => renderedHtml.value.length > 0);

const resolvedAriaLabel = computed<string>(() =>
  props.ariaLabel || t("common.help.button.ariaLabel"),
);
const resolvedTitle = computed<string>(() =>
  props.title || t("common.help.title"),
);
</script>

<template>
  <!-- eslint-disable vue/no-v-html -- content is authored in-repo Markdown
       rendered through the project's `renderMarkdown` (marked + hljs +
       DOMPurify) pipeline; anchor rel/target are re-forced above. Not an
       XSS vector. -->
  <button
    type="button"
    class="help-btn"
    :class="[`help-btn--${size}`]"
    :aria-label="resolvedAriaLabel"
    :title="resolvedAriaLabel"
    :data-help-key="docKey"
    @click="show"
  >
    <!-- Inline ℹ️ SVG (Lucide `Info` glyph). Uses currentColor so hover /
         focus tokens flow through without a colour override. -->
    <svg
      class="help-btn__icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="16" x2="12" y2="12" />
      <line x1="12" y1="8" x2="12.01" y2="8" />
    </svg>
  </button>

  <ChannelInfoDialog
    :open="open"
    icon="ℹ️"
    :title="resolvedTitle"
    :subtitle="externalUrl || ''"
    @close="hide"
  >
    <div
      v-if="hasContent"
      class="help-content"
      v-html="renderedHtml"
    />
    <div
      v-else
      class="help-content help-content--fallback"
      role="alert"
    >
      <p>{{ t("common.help.loadFailed") }}</p>
      <p
        v-if="externalUrl"
        class="help-content__fallback-link"
      >
        <a
          :href="externalUrl"
          target="_blank"
          rel="noopener noreferrer"
        >{{ t("common.help.viewOfficial") }} ↗</a>
      </p>
    </div>

    <div
      v-if="externalUrl && hasContent"
      class="help-content__external"
    >
      <a
        class="btn btn-ghost btn-sm help-content__external-btn"
        :href="externalUrl"
        target="_blank"
        rel="noopener noreferrer"
      >
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
          <polyline points="15 3 21 3 21 9" />
          <line x1="10" y1="14" x2="21" y2="3" />
        </svg>
        {{ t("common.help.viewOfficial") }}
      </a>
    </div>
  </ChannelInfoDialog>
  <!-- eslint-enable vue/no-v-html -->
</template>

<style scoped>
/*
 * The button is a circular icon-only affordance. Visual size follows the
 * `size` prop but the interactive hit area is always ≥ 32×32 (WCAG 2.5.5).
 * All colours come from CSS tokens so light/dark themes both work.
 */
.help-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: 50%;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition:
    background var(--transition, 0.15s ease),
    color var(--transition, 0.15s ease),
    border-color var(--transition, 0.15s ease);
  flex-shrink: 0;
}

.help-btn:hover {
  background: var(--bg-hover, var(--bg-tertiary));
  color: var(--text-primary);
}

.help-btn:focus-visible {
  outline: none;
  border-color: var(--accent, currentColor);
  box-shadow: 0 0 0 2px var(--focus-ring, rgba(59, 130, 246, 0.35));
}

.help-btn:active {
  background: var(--bg-tertiary);
}

.help-btn__icon {
  width: 16px;
  height: 16px;
}

.help-btn--md .help-btn__icon {
  width: 20px;
  height: 20px;
}

/*
 * Rendered Markdown body — inherits tokens rather than hard-coding colours
 * so we're consistent with chat message rendering.
 */
.help-content {
  font-size: var(--text-sm);
  color: var(--text-primary);
  line-height: 1.55;
  word-break: break-word;
}

.help-content :deep(h1),
.help-content :deep(h2),
.help-content :deep(h3),
.help-content :deep(h4) {
  color: var(--text-primary);
  margin: var(--space-3) 0 var(--space-2);
  font-weight: 600;
  line-height: 1.3;
}

.help-content :deep(h1) { font-size: var(--text-md); }
.help-content :deep(h2) { font-size: var(--text-md); }
.help-content :deep(h3) { font-size: var(--text-sm); text-transform: uppercase; letter-spacing: 0.02em; color: var(--text-secondary); }
.help-content :deep(h4) { font-size: var(--text-sm); }

.help-content :deep(p) {
  margin: 0 0 var(--space-2);
}

.help-content :deep(ul),
.help-content :deep(ol) {
  margin: var(--space-2) 0;
  padding-left: 1.25em;
}

.help-content :deep(li) {
  margin-bottom: 4px;
}

.help-content :deep(code) {
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  padding: 0 4px;
  font-family: var(--font-mono, "SFMono-Regular", Menlo, Consolas, monospace);
  font-size: 0.92em;
  color: var(--text-primary);
}

.help-content :deep(pre) {
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: var(--radius-md, 6px);
  padding: var(--space-2) var(--space-3);
  overflow-x: auto;
}

.help-content :deep(pre) code {
  background: transparent;
  border: none;
  padding: 0;
}

.help-content :deep(a) {
  color: var(--accent, #2563eb);
  text-decoration: none;
}

.help-content :deep(a:hover) {
  text-decoration: underline;
}

/*
 * SVG figures embedded via `![alt](/help-images/…svg)` must scale down on
 * narrow modals and never overflow horizontally.
 */
.help-content :deep(img) {
  max-width: 100%;
  height: auto;
  display: block;
  margin: var(--space-3) auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-md, 6px);
  background: var(--bg-primary, var(--bg-secondary));
  padding: var(--space-2);
}

.help-content :deep(blockquote) {
  border-left: 3px solid var(--accent, var(--border));
  margin: var(--space-2) 0;
  padding: 4px var(--space-3);
  color: var(--text-secondary);
  background: var(--bg-tertiary);
  border-radius: 0 var(--radius-sm, 4px) var(--radius-sm, 4px) 0;
}

.help-content :deep(hr) {
  border: none;
  border-top: 1px solid var(--border);
  margin: var(--space-3) 0;
}

.help-content :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: var(--space-2) 0;
  font-size: 0.95em;
}

.help-content :deep(th),
.help-content :deep(td) {
  border: 1px solid var(--border);
  padding: 4px 8px;
  text-align: left;
}

.help-content :deep(th) {
  background: var(--bg-tertiary);
  color: var(--text-primary);
  font-weight: 600;
}

.help-content--fallback {
  padding: var(--space-3);
  border: 1px dashed var(--border);
  border-radius: var(--radius-md, 6px);
  color: var(--text-muted);
  text-align: center;
}

.help-content__fallback-link {
  margin-top: var(--space-2);
}

.help-content__external {
  margin-top: var(--space-4);
  padding-top: var(--space-3);
  border-top: 1px dashed var(--border);
  display: flex;
  justify-content: flex-end;
}

.help-content__external-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
</style>
