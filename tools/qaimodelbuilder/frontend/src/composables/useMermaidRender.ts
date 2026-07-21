// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Vue integration for native Mermaid rendering in committed Markdown.
 *
 * Usage: in a component that renders committed Markdown via `v-html`, obtain a
 * template ref to the container element and pass it (plus a reactive content
 * key + a render-enabled flag) to `useMermaidRender`. The hook runs
 * `renderMermaid` AFTER Vue has flushed the DOM (so the `v-html` markup
 * exists), re-running it when:
 *   - the element first mounts with mermaid markers,
 *   - the committed content changes (new / edited message),
 *   - the theme switches (so the diagram re-renders with theme-correct colours).
 *
 * It deliberately does NOT run while streaming: the caller passes
 * `enabled = false` for the live bubble, so the user sees the raw code block
 * until the turn commits, then the diagram renders (per the product decision).
 *
 * The actual SVG generation + sanitisation lives in `markdown-mermaid.ts`
 * (separate SVG DOMPurify profile); this hook only owns the Vue lifecycle:
 * debounced post-flush scheduling, an abortable token that supersedes an
 * in-flight render when inputs change, and teardown on unmount.
 */
import {
  nextTick,
  onBeforeUnmount,
  watch,
  type Ref,
  type WatchSource,
} from "vue";
import { storeToRefs } from "pinia";

import { useUiStore } from "@/stores/ui";
import {
  hasMermaid,
  renderMermaid,
  type MermaidLabels,
} from "@/composables/markdown-mermaid";

export interface UseMermaidRenderOptions {
  /**
   * Reactive source(s) that change when the rendered Markdown content changes
   * (e.g. the committed message content, or a per-turn content key). A change
   * schedules a re-render.
   */
  readonly content: WatchSource | WatchSource[];
  /**
   * Whether Mermaid rendering is active. Pass a ref that is `false` while the
   * bubble is streaming and flips to `true` once committed. Defaults to always
   * enabled when omitted.
   */
  readonly enabled?: Ref<boolean>;
  /** i18n label provider (called lazily so locale switches are picked up). */
  readonly labels?: () => Partial<MermaidLabels>;
}

export function useMermaidRender(
  container: Ref<HTMLElement | null | undefined>,
  options: UseMermaidRenderOptions,
): void {
  const ui = useUiStore();
  const { resolvedTheme } = storeToRefs(ui);

  // A mutable token: each scheduled render captures the CURRENT token; when a
  // newer render is scheduled we flip `aborted` on the old one so an in-flight
  // async render bails out instead of writing stale SVG (theme race / unmount).
  let token = { aborted: false };

  function schedule(): void {
    token.aborted = true;
    const current = { aborted: false };
    token = current;

    void nextTick(() => {
      if (current.aborted) return;
      const root = container.value;
      if (!root || !root.isConnected) return;
      if (options.enabled && !options.enabled.value) return;
      if (!hasMermaid(root)) return;
      void renderMermaid(root, current, options.labels?.());
    });
  }

  const sources: WatchSource[] = [
    ...(Array.isArray(options.content) ? options.content : [options.content]),
    resolvedTheme,
    ...(options.enabled ? [options.enabled] : []),
  ];

  watch(sources, schedule, { immediate: true, flush: "post" });

  onBeforeUnmount(() => {
    token.aborted = true;
  });
}
