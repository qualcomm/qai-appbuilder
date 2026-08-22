<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
 * PipelineCard.vue — MB Pro pipeline/v1 dependency graph card.
 *
 * Rendered inside the assistant message's tool-call area (ChatMessageList →
 * ToolCallList) in place of the generic ``ToolExecPanel`` whenever
 * ``call.toolName === "show_pipeline"``.
 *
 * The upstream ``show_pipeline`` handler returns text produced by mbp's
 * ``_render_pipeline_summary`` — a bespoke half-formatted DSL (2-space
 * indent + ▶ + single-\n lines, plus a 【依赖边...】 subsection). Marked's
 * GFM mode with ``breaks: false`` would collapse the whole thing into one
 * ``<p>``. mbp keeps that format on purpose (it's also fed into debug_agent
 * LLM context) so we normalize on the QAI side: DSL → valid GFM (``-`` list
 * items + ``### `` subsection + blank-line separation) before rendering.
-->
<script setup lang="ts">
import { computed } from "vue";
import { renderMarkdown } from "@/composables/markdown";

interface Props {
  result?: string;
}

const props = withDefaults(defineProps<Props>(), { result: "" });

/**
 * mbp _render_pipeline_summary emits lines like:
 *   ## 已校验流程图（pipeline/v1，N step / M 边...）
 *     ▶ step_id  (entry.py)  ↑产: a、b、c
 *     ▶ step_id2  (entry2.py)
 *   【依赖边（写→读，file:line 已核真有读写动词）】
 *     from → to  ::artifact  [写 f:l / 读 f:l]
 *
 * Marked (gfm:true, breaks:false) treats consecutive non-empty lines as one
 * paragraph, so the whole graph renders as a single ``<p>`` blob. This turns
 * each ▶ line into a ``-`` list item, promotes 【依赖边...】 to ``###``,
 * and inserts blank lines around block boundaries.
 */
function normalizeMbpPipelineDsl(text: string): string {
  const lines = text.split("\n");
  const out: string[] = [];
  let inEdgeSection = false;

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");

    // Step row: "  ▶ step_id  (entry)  ↑产: a、b、c"
    const stepMatch = line.match(/^\s*▶\s*(\S+)\s*(\([^)]*\))?\s*(.*)$/);
    if (stepMatch !== null) {
      const [, id, entry, rest] = stepMatch;
      const parts = [`**${id}**`];
      if (entry !== undefined && entry !== "") parts.push(`\`${entry}\``);
      if (rest !== undefined && rest !== "") parts.push(rest);
      if (out.length > 0 && out[out.length - 1] !== "" && !out[out.length - 1]!.startsWith("- ")) {
        out.push("");
      }
      out.push(`- ${parts.join("  ")}`);
      continue;
    }

    // Edge section header (upstream uses 【...】 not markdown heading).
    if (/^【依赖边/.test(line.trimStart())) {
      const heading = line.trimStart().replace(/^【/, "").replace(/】\s*$/, "");
      if (out.length > 0 && out[out.length - 1] !== "") out.push("");
      out.push(`### ${heading}`);
      out.push("");
      inEdgeSection = true;
      continue;
    }

    // Edge row: "  from → to  ::artifact  [写 f:l / 读 f:l]"
    if (inEdgeSection) {
      const edgeMatch = line.match(
        /^\s*(\S+)\s*→\s*(\S+)\s*::\s*(\S+)\s*(\[.*\])?\s*$/,
      );
      if (edgeMatch !== null) {
        const [, from, to, artifact, evidence] = edgeMatch;
        const suffix = evidence !== undefined && evidence !== "" ? `  ${evidence}` : "";
        out.push(`- \`${from}\` → \`${to}\`  ::${artifact}${suffix}`);
        continue;
      }
    }

    out.push(line);
  }

  return out.join("\n");
}

const html = computed<string>(() =>
  props.result ? renderMarkdown(normalizeMbpPipelineDsl(props.result)) : "",
);

const isEmpty = computed(() => !props.result?.trim());
</script>

<template>
  <section class="pipeline-card" data-testid="pipeline-card">
    <header class="pipeline-card__header">
      <span class="pipeline-card__icon" aria-hidden="true">🧬</span>
      <span class="pipeline-card__title">Pipeline / 依赖图</span>
    </header>

    <div v-if="isEmpty" class="pipeline-card__empty">（暂无 pipeline 流程图）</div>

    <!-- eslint-disable-next-line vue/no-v-html -->
    <div
      v-else
      class="pipeline-card__body markdown-body"
      v-html="html"
    />
  </section>
</template>

<style scoped>
.pipeline-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  margin: 6px 0;
  border-radius: 8px;
  border: 1px solid var(--color-border, rgba(0, 0, 0, 0.12));
  background: var(--color-surface, rgba(255, 255, 255, 0.6));
}

.pipeline-card__header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text, inherit);
}

.pipeline-card__icon {
  font-size: 16px;
  line-height: 1;
}

.pipeline-card__empty {
  font-size: 12px;
  color: var(--color-text-muted, #6b7280);
  font-style: italic;
}

.pipeline-card__body {
  font-size: 12px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.pipeline-card__body :deep(h2) {
  font-size: 13px;
  font-weight: 600;
  margin: 4px 0 6px;
  color: var(--color-text, inherit);
}

.pipeline-card__body :deep(p) {
  margin: 4px 0;
}

.pipeline-card__body :deep(pre),
.pipeline-card__body :deep(code) {
  font-size: 11px;
  background: var(--color-surface, rgba(0, 0, 0, 0.04));
  padding: 2px 4px;
  border-radius: 3px;
}
</style>
