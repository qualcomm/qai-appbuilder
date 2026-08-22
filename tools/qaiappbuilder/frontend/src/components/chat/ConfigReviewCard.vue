<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
 * ConfigReviewCard.vue — MB Pro's client-review gate card.
 *
 * Rendered inside the assistant message's tool-call area (ChatMessageList →
 * ToolCallList) in place of the generic ``ToolExecPanel`` whenever
 * ``call.toolName === "config_review"`` — mirroring how ``todowrite`` renders
 * ``TaskListCard`` and ``question`` renders ``ChatQuestionCard``.
 *
 * Payload contract (upstream ``config_review_needed`` event, translated by
 * ``mb_pro_mapper.py``):
 *   {
 *     review_fields: {
 *       job_id, constraints,
 *       platform, model_name, model_spec,
 *       paths: { 输入, 输出, SDK, dataset, notebook },
 *       params: { CONTEXT_LENGTH, ARN, ..., 位宽 },
 *       input_paths: [ { name, path, ok, detail, kind }, ... ],
 *     },
 *     countdown_sec: number,            // initial 5-min timer, seconds
 *     countdown_anchor_ms: number,      // server epoch ms when event was minted
 *     notebook_repo?: string,
 *   }
 *
 * The card is a self-contained visual replacement for the raw fold-out
 * tool card: platform / model line up top for at-a-glance identity, a
 * two-column table for paths + params, a highlighted countdown clock, and
 * (optional) an input-paths verification list with ✅/❌ per entry.
 *
 * Countdown behaviour: front end drives its own tick loop (setInterval,
 * 1 s cadence). Anchor is the server-supplied ``countdown_anchor_ms``
 * epoch stamp so an SSE arrival delay does not skew the initial reading.
 * The mapper drops the upstream ``config_review_tick`` events entirely
 * (see ``mb_pro_mapper.py`` for the rationale). When the clock hits 0
 * the card just freezes at ``0:00`` — the actual timeout logic runs on
 * the MB Pro server and will synthesize a confirmation message, we only
 * mirror the visual countdown here.
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";

interface InputPathEntry {
  name?: unknown;
  path?: unknown;
  ok?: unknown;
  detail?: unknown;
  kind?: unknown;
}

interface ReviewFields {
  job_id?: unknown;
  constraints?: unknown;
  platform?: unknown;
  model_name?: unknown;
  model_spec?: unknown;
  paths?: Record<string, unknown>;
  params?: Record<string, unknown>;
  input_paths?: InputPathEntry[];
}

interface Props {
  args?: Record<string, unknown>;
}

const props = withDefaults(defineProps<Props>(), {
  args: () => ({}),
});

const { t } = useI18n();

/** Safe-cast the raw ``args.review_fields`` payload. */
const rf = computed<ReviewFields>(() => {
  const raw = props.args?.review_fields;
  return (raw !== null && typeof raw === "object" ? raw : {}) as ReviewFields;
});

const notebookRepo = computed<string>(() => {
  const v = props.args?.notebook_repo;
  return typeof v === "string" ? v : "";
});

const jobId = computed<string>(() => {
  const v = rf.value.job_id;
  return typeof v === "string" ? v : "";
});

const platform = computed<string>(() =>
  typeof rf.value.platform === "string" ? rf.value.platform : "",
);

const modelName = computed<string>(() =>
  typeof rf.value.model_name === "string" ? rf.value.model_name : "",
);

const modelSpec = computed<string>(() =>
  typeof rf.value.model_spec === "string" ? rf.value.model_spec : "",
);

const constraints = computed<string>(() =>
  typeof rf.value.constraints === "string" ? rf.value.constraints.trim() : "",
);

/** Convert the paths / params dicts into stable [label, value][] rows,
 *  filtering out ``(未填)``-placeholder values so a Chinese ellipsis
 *  fill-in-later marker does not visually dominate the table. Null / undef
 *  entries collapse the same way. */
function toEntries(obj: unknown): Array<[string, string]> {
  if (obj === null || typeof obj !== "object") return [];
  const out: Array<[string, string]> = [];
  for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
    const s = v === null || v === undefined ? "" : String(v);
    if (!s || s === "(未填)") continue;
    out.push([k, s]);
  }
  return out;
}

const pathEntries = computed(() => toEntries(rf.value.paths));
const paramEntries = computed(() => toEntries(rf.value.params));

const inputPaths = computed<InputPathEntry[]>(() =>
  Array.isArray(rf.value.input_paths) ? rf.value.input_paths : [],
);

// ── Countdown clock ───────────────────────────────────────────────────
// Local tick loop; anchor is the server-supplied epoch stamp so a slow
// SSE delivery does not skew the initial reading. Recomputes on every
// tick so late remounts / v-if toggles resync automatically.
const initialCountdownSec = computed<number>(() => {
  const v = props.args?.countdown_sec;
  return typeof v === "number" && v > 0 ? v : 0;
});

const anchorMs = computed<number>(() => {
  const v = props.args?.countdown_anchor_ms;
  return typeof v === "number" && v > 0 ? v : Date.now();
});

const now = ref(Date.now());
let tickHandle: ReturnType<typeof setInterval> | null = null;

onMounted(() => {
  if (initialCountdownSec.value <= 0) return;
  tickHandle = setInterval(() => {
    now.value = Date.now();
  }, 1000);
});

onBeforeUnmount(() => {
  if (tickHandle !== null) {
    clearInterval(tickHandle);
    tickHandle = null;
  }
});

const remainingSec = computed<number>(() => {
  if (initialCountdownSec.value <= 0) return 0;
  const elapsedMs = Math.max(0, now.value - anchorMs.value);
  const remaining = initialCountdownSec.value - Math.floor(elapsedMs / 1000);
  return Math.max(0, remaining);
});

const countdownDisplay = computed<string>(() => {
  const s = remainingSec.value;
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${r.toString().padStart(2, "0")}`;
});

const countdownIsUrgent = computed<boolean>(() => remainingSec.value <= 30);
const countdownIsExpired = computed<boolean>(() => remainingSec.value <= 0);
</script>

<template>
  <section
    class="config-review-card"
    :class="{
      'config-review-card--urgent': countdownIsUrgent && !countdownIsExpired,
      'config-review-card--expired': countdownIsExpired,
    }"
    data-testid="config-review-card"
  >
    <!-- ── Header: identity + countdown ─────────────────────────── -->
    <header class="config-review-card__header">
      <div class="config-review-card__title">
        <span class="config-review-card__icon" aria-hidden="true">⚡</span>
        <span class="config-review-card__title-text">
          {{ t("mbPro.configReview.title", "配置确认") }}
        </span>
      </div>
      <div
        v-if="initialCountdownSec > 0"
        class="config-review-card__countdown"
        :aria-label="t('mbPro.configReview.countdownLabel', '剩余确认时间')"
      >
        <span class="config-review-card__countdown-icon" aria-hidden="true">⏱</span>
        <span class="config-review-card__countdown-value">{{ countdownDisplay }}</span>
      </div>
    </header>

    <!-- ── Identity strip: platform · model ─────────────────────── -->
    <div class="config-review-card__identity">
      <div v-if="platform" class="config-review-card__identity-row">
        <span class="config-review-card__label">
          {{ t("mbPro.configReview.platform", "平台") }}
        </span>
        <span class="config-review-card__value config-review-card__value--mono">
          {{ platform }}
        </span>
      </div>
      <div v-if="modelName" class="config-review-card__identity-row">
        <span class="config-review-card__label">
          {{ t("mbPro.configReview.model", "模型") }}
        </span>
        <span class="config-review-card__value">
          {{ modelName }}
          <span v-if="modelSpec" class="config-review-card__spec">{{ modelSpec }}</span>
        </span>
      </div>
    </div>

    <!-- ── User constraints (highest-authority text) ────────────── -->
    <div v-if="constraints" class="config-review-card__constraints">
      <span class="config-review-card__constraints-badge">
        {{ t("mbPro.configReview.userConstraint", "用户约束") }}
      </span>
      <span class="config-review-card__constraints-text">{{ constraints }}</span>
    </div>

    <!-- ── Two-column: paths + params ───────────────────────────── -->
    <div class="config-review-card__body">
      <div v-if="pathEntries.length > 0" class="config-review-card__column">
        <h3 class="config-review-card__column-title">
          {{ t("mbPro.configReview.paths", "路径") }}
        </h3>
        <dl class="config-review-card__dl">
          <template v-for="[k, v] in pathEntries" :key="`p-${k}`">
            <dt class="config-review-card__dt">{{ k }}</dt>
            <dd class="config-review-card__dd config-review-card__dd--mono">{{ v }}</dd>
          </template>
        </dl>
      </div>
      <div v-if="paramEntries.length > 0" class="config-review-card__column">
        <h3 class="config-review-card__column-title">
          {{ t("mbPro.configReview.params", "参数") }}
        </h3>
        <dl class="config-review-card__dl">
          <template v-for="[k, v] in paramEntries" :key="`k-${k}`">
            <dt class="config-review-card__dt">{{ k }}</dt>
            <dd class="config-review-card__dd">{{ v }}</dd>
          </template>
        </dl>
      </div>
    </div>

    <!-- ── Input-path verification (sample-level check) ─────────── -->
    <div v-if="inputPaths.length > 0" class="config-review-card__inputs">
      <h3 class="config-review-card__column-title">
        {{ t("mbPro.configReview.inputPaths", "运行时输入路径") }}
      </h3>
      <ul class="config-review-card__input-list">
        <li
          v-for="(ip, i) in inputPaths"
          :key="`ip-${i}`"
          class="config-review-card__input-item"
          :class="{
            'config-review-card__input-item--ok': Boolean(ip.ok),
            'config-review-card__input-item--fail': ip.ok === false,
          }"
        >
          <span class="config-review-card__input-status" aria-hidden="true">
            {{ ip.ok ? "✅" : "❌" }}
          </span>
          <span class="config-review-card__input-name">{{ String(ip.name ?? "") }}</span>
          <span class="config-review-card__input-path">{{ String(ip.path ?? "") }}</span>
          <span
            v-if="ip.detail"
            class="config-review-card__input-detail"
          >{{ String(ip.detail) }}</span>
        </li>
      </ul>
    </div>

    <!-- ── Footer: notebook_repo + job_id (small print) ─────────── -->
    <footer
      v-if="notebookRepo || jobId"
      class="config-review-card__footer"
    >
      <span v-if="notebookRepo" class="config-review-card__footer-item">
        <span class="config-review-card__label">
          {{ t("mbPro.configReview.notebook", "Notebook") }}
        </span>
        <code>{{ notebookRepo }}</code>
      </span>
      <span v-if="jobId" class="config-review-card__footer-item">
        <span class="config-review-card__label">Job ID</span>
        <code>{{ jobId }}</code>
      </span>
    </footer>

    <!-- ── Confirmation hint (no button — user types 确认 in chat) ─ -->
    <div class="config-review-card__hint">
      {{
        countdownIsExpired
          ? t(
              "mbPro.configReview.hintExpired",
              "倒计时结束，如未回应系统将自动开跑；如需修改请在下方输入。",
            )
          : t(
              "mbPro.configReview.hint",
              "在下方输入「确认」开跑，或直接说明要改的参数。",
            )
      }}
    </div>
  </section>
</template>

<style scoped>
.config-review-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px 16px;
  margin: 8px 0;
  border-radius: 10px;
  border: 2px solid var(--color-primary, #4f46e5);
  background: var(--color-primary-soft, rgba(79, 70, 229, 0.06));
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}

.config-review-card--urgent {
  border-color: var(--color-warning, #f59e0b);
  background: rgba(245, 158, 11, 0.08);
}

.config-review-card--expired {
  border-color: var(--color-text-muted, #9ca3af);
  background: rgba(156, 163, 175, 0.08);
}

.config-review-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.config-review-card__title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text, inherit);
}

.config-review-card__icon {
  font-size: 18px;
  line-height: 1;
}

.config-review-card__countdown {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 999px;
  background: var(--color-primary, #4f46e5);
  color: #fff;
  font-size: 13px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  min-width: 68px;
  justify-content: center;
}

.config-review-card--urgent .config-review-card__countdown {
  background: var(--color-warning, #f59e0b);
  animation: config-review-pulse 1s ease-in-out infinite alternate;
}

.config-review-card--expired .config-review-card__countdown {
  background: var(--color-text-muted, #6b7280);
}

@keyframes config-review-pulse {
  from { transform: scale(1); }
  to { transform: scale(1.05); }
}

.config-review-card__identity {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--color-surface, rgba(255, 255, 255, 0.6));
}

.config-review-card__identity-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-size: 13px;
}

.config-review-card__label {
  min-width: 56px;
  color: var(--color-text-muted, #6b7280);
  font-size: 12px;
  font-weight: 500;
}

.config-review-card__value {
  flex: 1;
  color: var(--color-text, inherit);
  word-break: break-all;
}

.config-review-card__value--mono {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 12px;
}

.config-review-card__spec {
  margin-left: 8px;
  color: var(--color-text-muted, #6b7280);
  font-size: 11px;
}

.config-review-card__constraints {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 6px;
  background: rgba(59, 130, 246, 0.08);
  border-left: 3px solid var(--color-info, #3b82f6);
  font-size: 13px;
}

.config-review-card__constraints-badge {
  flex-shrink: 0;
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--color-info, #3b82f6);
  color: #fff;
  font-size: 11px;
  font-weight: 500;
}

.config-review-card__constraints-text {
  color: var(--color-text, inherit);
  white-space: pre-wrap;
  word-break: break-word;
}

.config-review-card__body {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

@media (max-width: 640px) {
  .config-review-card__body {
    grid-template-columns: 1fr;
  }
}

.config-review-card__column {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.config-review-card__column-title {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--color-text-muted, #6b7280);
  margin: 0;
}

.config-review-card__dl {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 4px 10px;
  margin: 0;
  font-size: 12px;
}

.config-review-card__dt {
  color: var(--color-text-muted, #6b7280);
  font-weight: 500;
}

.config-review-card__dd {
  margin: 0;
  color: var(--color-text, inherit);
  word-break: break-all;
}

.config-review-card__dd--mono {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11px;
}

.config-review-card__inputs {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.config-review-card__input-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.config-review-card__input-item {
  display: grid;
  grid-template-columns: auto max-content 1fr;
  align-items: baseline;
  gap: 8px;
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 12px;
  background: var(--color-surface, rgba(255, 255, 255, 0.4));
}

.config-review-card__input-item--fail {
  background: rgba(239, 68, 68, 0.08);
  color: var(--color-error, #b91c1c);
}

.config-review-card__input-status {
  font-size: 13px;
}

.config-review-card__input-name {
  font-weight: 500;
}

.config-review-card__input-path {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11px;
  color: var(--color-text-muted, #6b7280);
  word-break: break-all;
}

.config-review-card__input-detail {
  grid-column: 1 / -1;
  padding-left: 26px;
  font-size: 11px;
  color: var(--color-text-muted, #6b7280);
}

.config-review-card__footer {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  padding-top: 6px;
  border-top: 1px dashed var(--color-border, rgba(0, 0, 0, 0.08));
  font-size: 11px;
}

.config-review-card__footer-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.config-review-card__footer-item code {
  font-size: 11px;
  padding: 1px 4px;
  border-radius: 3px;
  background: var(--color-surface, rgba(0, 0, 0, 0.04));
  word-break: break-all;
}

.config-review-card__hint {
  padding: 6px 10px;
  border-radius: 4px;
  background: var(--color-primary, #4f46e5);
  color: #fff;
  font-size: 12px;
  text-align: center;
  opacity: 0.92;
}

.config-review-card--urgent .config-review-card__hint {
  background: var(--color-warning, #f59e0b);
}

.config-review-card--expired .config-review-card__hint {
  background: var(--color-text-muted, #6b7280);
}
</style>
