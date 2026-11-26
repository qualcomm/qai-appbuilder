<script setup lang="ts">
import { computed } from "vue";
import type { TaskSummary } from "@/stores/_chatTabsTypes";

const props = defineProps<{ summary: TaskSummary }>();

const fmt = (n: number) => n.toLocaleString("en-US");
const duration = computed(() => {
  const s = props.summary.duration_ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s - m * 60)}s`;
});
// Cache read / write are PORTIONS of Input (already counted in it), so they
// are shown inside Input's parentheses — never as separate addends.
const cacheParts = computed(() => {
  const parts: string[] = [];
  if (props.summary.cache_read_tokens > 0) {
    parts.push(`cache read ${fmt(props.summary.cache_read_tokens)}`);
  }
  if (props.summary.cache_write_tokens > 0) {
    parts.push(`cache write ${fmt(props.summary.cache_write_tokens)}`);
  }
  return parts;
});
const subs = computed(() => props.summary.sub_agents ?? []);
const subTotal = computed(() => subs.value.reduce((a, b) => a + b.tokens, 0));
</script>

<template>
  <div class="task-summary-card" data-testid="task-summary-card">
    <div class="ts-head">
      <span v-if="summary.complete === false" class="ts-title ts-running">
        SUB-AGENTS RUNNING
      </span>
      <span v-else class="ts-title">TASK COMPLETE</span>
      <span v-if="summary.model" class="ts-model">{{ summary.model }}</span>
    </div>
    <div class="ts-stats">
      <div>
        <div class="ts-label">Tokens consumed</div>
        <div class="ts-value">{{ fmt(summary.total_tokens) }}</div>
      </div>
      <div>
        <div class="ts-label">Execution time</div>
        <div class="ts-value">{{ duration }}</div>
      </div>
    </div>
    <div class="ts-detail">
      Input {{ fmt(summary.input_tokens) }}<template v-if="cacheParts.length > 0">
        (incl. {{ cacheParts.join(", ") }})</template>
      · Output {{ fmt(summary.output_tokens) }}
    </div>
    <details v-if="subs.length > 0" class="ts-subs">
      <summary>
        Main {{ fmt(summary.main_tokens) }} · {{ subs.length }} sub-agent(s)
        {{ fmt(subTotal) }}
      </summary>
      <div v-for="(s, i) in subs" :key="s.id ?? i" class="ts-sub-row">
        <span>{{ s.name }}<template v-if="s.running"> · running</template></span>
        <span>{{ fmt(s.tokens) }}<template v-if="s.rounds"> · {{ s.rounds }} rounds</template></span>
      </div>
    </details>
  </div>
</template>

<style scoped>
.task-summary-card {
  display: inline-block;
  min-width: 320px;
  margin: 8px 0;
  padding: 12px 16px;
  border-radius: 10px;
  background: rgba(128, 128, 128, 0.14);
  font-family: ui-monospace, Consolas, monospace;
}
.ts-head { display: flex; justify-content: space-between; gap: 24px; font-size: 12px; }
.ts-title { color: #3fb950; font-weight: 700; letter-spacing: 0.04em; }
.ts-running { color: #d29922; }
.ts-model { opacity: 0.6; }
.ts-stats { display: flex; gap: 32px; margin: 8px 0; }
.ts-label { font-size: 12px; opacity: 0.65; }
.ts-value { font-size: 20px; font-weight: 700; }
.ts-detail { font-size: 12px; opacity: 0.65; }
.ts-subs { margin-top: 6px; font-size: 12px; opacity: 0.8; }
.ts-sub-row { display: flex; justify-content: space-between; padding-left: 12px; }
</style>
