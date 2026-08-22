<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->
<script setup lang="ts">
/**
 * SidebarQuotaGauge — provider-aware quota readout.
 *
 * Answers "how much have I got left?" for the provider the SELECTED model
 * belongs to, and renders one of two shapes because the two metered providers
 * count fundamentally differently:
 *
 * * **qai-service** — a single per-user token pool. One bar.
 * * **qgenie** — TWO independent allowances (`API/SDK` and `IDE/CLI`) that
 *   QGenie bills to based purely on the outbound `User-Agent`. They carry
 *   different limits and either can run dry alone, so a single bar cannot
 *   describe them: stacking two separate scales would be arithmetic nonsense.
 *   Rendered as one bar split down the middle, each half its own 0-100% scale,
 *   with the currently-billed side highlighted.
 *
 * Any OTHER provider (a user's own gateway) renders NOTHING: we have no way to
 * read its quota, and an empty or zeroed bar would just invite "why is my
 * quota 0?". Same reason the whole widget hides when no balance is known and
 * when the rail is collapsed.
 *
 * Data flow — neither path polls:
 * * qai-service (`stores/quota.ts`): seeded at mount, then updated for free by
 *   the `quota_usage` stream frame on every answer.
 * * qgenie (`stores/qgenieQuota.ts`): read when the user switches onto a QGenie
 *   model and again after each turn ends. QGenie throttles its quota endpoints
 *   at ~60 s, so a polling loop would spend the budget the exhaustion check
 *   needs while adding nothing.
 */
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { IS_INTERNAL } from "@/edition";
import QGenieQuotaDialog from "@/components/layout/QGenieQuotaDialog.vue";
import { useConfig } from "@/composables/useConfig";
import { fmtTokenCount } from "@/utils/contextBadge";
import { useChatTabsStore } from "@/stores/chatTabs";
import {
  DEFAULT_WARN_PERCENT,
  type TrafficClass,
  useQGenieQuotaStore,
} from "@/stores/qgenieQuota";
import { useQuotaStore } from "@/stores/quota";

const props = defineProps<{
  /** True while the sidebar is in its icon-only rail state. */
  collapsed?: boolean;
}>();

const { t, locale } = useI18n();

/** Per-model breakdown dialog, opened by clicking the QGenie gauge. */
const dialogOpen = ref(false);
const quota = useQuotaStore();
const qgenie = useQGenieQuotaStore();
const tabs = useChatTabsStore();

/** Provider slug of the selected model; drives which shape (if any) renders. */
const provider = computed(() => tabs.activeTab?.modelProvider ?? "");
const modelId = computed(() => tabs.activeTab?.modelId ?? "");

/**
 * Provider genuinely unknown — NOT the same as "a third-party provider".
 *
 * `modelProvider` is empty in routine states, not just transiently: the
 * persisted tab layout does not carry it (`chatTabs.restoreLayout` restores
 * only conversationId / title / sessionToolOverride), and the watcher that
 * backfills it lives in the composer, which is not mounted outside `/chat`.
 * This gauge sits in the sidebar, so it renders on every route.
 *
 * Treating "" as "hide" would therefore delete the qai-service bar after any
 * refresh and on every non-chat route — a regression, since before this
 * feature the bar rendered whenever a balance was known. `quota.available`
 * already self-certifies that the token pool is in play, so an unknown
 * provider falls back to that older, weaker condition.
 */
const providerUnknown = computed(() => provider.value === "");

/**
 * QGenie selected?
 *
 * `IS_INTERNAL` short-circuits first: the QGenie provider ships only with the
 * internal edition (`internal_config.toml` is excluded from the release
 * bundle), so on an external build there is nothing to meter and every QGenie
 * branch below collapses to a constant `false` at build time.
 */
const isQGenie = computed(() => IS_INTERNAL && provider.value === "qgenie");
const isQaiService = computed(() => provider.value === "qai-service");

/**
 * Seed the QGenie snapshot when the user lands on a QGenie model.
 *
 * Without this the gauge would be blank until the first turn finished, since
 * the only other refresh point is end-of-turn.
 */
watch(
  isQGenie,
  (active) => {
    if (active && !qgenie.available) void qgenie.refresh();
  },
  { immediate: true },
);

const showQaiService = computed(
  () =>
    (isQaiService.value || providerUnknown.value) &&
    quota.available &&
    props.collapsed !== true,
);
const showQGenie = computed(
  () =>
    isQGenie.value &&
    qgenie.available &&
    qgenie.quotaFor(modelId.value) !== null &&
    props.collapsed !== true,
);

const { config, fetchConfig } = useConfig();

onMounted(() => {
  // Both reads exist purely for the QGenie half, so skip them entirely on an
  // external build: no provider, no threshold to honour, no request to spend.
  if (!IS_INTERNAL) return;
  void fetchConfig();
  // Load the persisted per-model buckets so the highlighted half matches the
  // one requests will actually be billed to, not the default.
  void qgenie.hydrate();
});

/**
 * User-configured warn threshold (Settings → Agent), as a percent.
 *
 * Read from the live config rather than snapshotted at mount so changing it
 * binds on the next turn without a reload.
 */
const warnPercent = computed(() => {
  const chat = (config.value as Record<string, unknown> | null)?.chat;
  if (chat === null || typeof chat !== "object") return DEFAULT_WARN_PERCENT;
  const raw = (chat as Record<string, unknown>).qgenie_quota_warn_percent;
  return typeof raw === "number" && Number.isFinite(raw)
    ? raw
    : DEFAULT_WARN_PERCENT;
});

/**
 * Pending escalating warning, or null. Drives the dialog's decision mode.
 *
 * A dialog rather than a yes/no confirm because the real question is a
 * comparison — "this side has X left, the other has Y" — and the answer has
 * THREE outcomes (switch / spend the rest / drop the model), which a boolean
 * cannot express.
 */
const decision = ref<ReturnType<typeof qgenie.quotaDecision>>(null);

/**
 * After each turn, re-read the quota and escalate at most once per stage.
 *
 * `force` bypasses the backend cooldown deliberately: this must not be decided
 * on a stale reading, or we could offer a switch to a bucket that has since run
 * dry, or claim both are dry when one is free.
 *
 * The store owns the escalation bookkeeping (`quotaDecision` returns null when
 * the stage was already announced), which is what stops the "prompt after every
 * single turn once past the line" nagging.
 */
watch(
  () => tabs.activeTab?.qgenieQuotaCheckSignal,
  async (signal) => {
    if (signal === undefined || !isQGenie.value) return;
    const model = modelId.value;
    if (model === "") return;
    // Never interrupt with a second dialog while one is still open.
    if (dialogOpen.value) return;
    if (!(await qgenie.refresh(true))) return;

    const next = qgenie.quotaDecision(model, warnPercent.value);
    if (next === null) return;
    decision.value = next;
    dialogOpen.value = true;
  },
);

function closeDialog(): void {
  dialogOpen.value = false;
  decision.value = null;
}

/** "Keep using it" — the stage is already recorded, so this just dismisses. */
function onKeepGoing(): void {
  closeDialog();
}

async function onSwitchTo(traffic: TrafficClass): Promise<void> {
  const model = modelId.value;
  closeDialog();
  if (model !== "") await qgenie.switchClass(model, traffic);
}

/**
 * Set the class for an ARBITRARY model from the dialog's table.
 *
 * Does NOT close the dialog: the point of picking a non-selected model is to
 * set several up in one visit (a sub-agent's model is only reachable this way,
 * since the composer never selects it). The store serialises the writes.
 */
async function onSwitchModel(
  model: string,
  traffic: TrafficClass,
): Promise<void> {
  if (model !== "") await qgenie.switchClass(model, traffic);
}

/**
 * Severity of the remaining share, driving the bar colour.
 *
 * Thresholds are about *actionability*, not aesthetics: at 20 % a user still
 * has room to finish what they are doing but should plan (amber); at 5 % the
 * next long turn may well fail mid-answer, which is worth alarming about
 * (red).
 */
function severityOf(remainingPercent: number): "ok" | "low" | "critical" {
  if (remainingPercent <= 5) return "critical";
  if (remainingPercent <= 20) return "low";
  return "ok";
}

const severity = computed(() => severityOf(quota.remainingPercent));

/**
 * Compact token count — `694K`, `13.6M`.
 *
 * Shares the dialog's formatter: the gauge tooltip and the table it opens show
 * the same numbers, and rendering them two ways would read as two facts.
 */
function formatTokens(value: number): string {
  return fmtTokenCount(value);
}


const remainingText = computed(() =>
  formatTokens(quota.balance?.remaining ?? 0),
);
const allocatedText = computed(() =>
  formatTokens(quota.balance?.allocated ?? 0),
);

/**
 * Tooltip: the numbers plus the reset instant when the broker supplies one.
 * `reset_at` is the only place the *period* becomes concrete, so it is worth
 * surfacing — a user seeing "5 % left" wants to know whether that is until
 * tomorrow or until next month.
 */
const tooltip = computed(() => {
  const base = t("quota.tooltip", {
    remaining: remainingText.value,
    allocated: allocatedText.value,
  });
  const resetAt = quota.balance?.reset_at;
  if (typeof resetAt !== "string" || resetAt === "") return base;
  const parsed = new Date(resetAt);
  if (Number.isNaN(parsed.getTime())) return base;
  return `${base}\n${t("quota.resetsAt", { when: parsed.toLocaleString(locale.value) })}`;
});

// ─── QGenie dual-bucket view ──────────────────────────────────────────────

const CLASSES: readonly TrafficClass[] = ["Api", "UI"];

/** Which bucket requests currently go to; the other half renders dimmed. */
const activeClass = computed(() => qgenie.classFor(modelId.value));

/**
 * One descriptor per half of the split bar.
 *
 * `usedPercent` (not remaining) fills each half, matching how a quota console
 * reads: the bar grows as you spend.
 */
const qgenieHalves = computed(() =>
  CLASSES.map((traffic) => {
    const usedPercent = qgenie.ratioFor(modelId.value, traffic) * 100;
    return {
      traffic,
      label: t(`qgenieQuota.class.${traffic}`),
      usedPercent,
      severity: severityOf(100 - usedPercent),
      active: traffic === activeClass.value,
      exhausted: qgenie.exhaustedFor(modelId.value, traffic),
    };
  }),
);

function formatWindow(counter: { used: number; limit: number } | null | undefined): string {
  if (!counter || counter.limit <= 0) return t("qgenieQuota.unknown");
  const pct = ((counter.used / counter.limit) * 100).toFixed(2);
  return `${formatTokens(counter.used)} / ${formatTokens(counter.limit)} (${pct}%)`;
}

function formatUsd(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `$${value.toFixed(2)}`;
}

/**
 * Tooltip: both buckets' daily + weekly windows, then account spend.
 *
 * The spend line is explicitly labelled as the total across ALL QGenie models
 * because it cannot be attributed per model — QGenie's observe-only writer does
 * not store model identifiers. Without that label a reader would take the
 * figure for the selected model's cost.
 */
const qgenieTooltip = computed(() => {
  const quotaEntry = qgenie.quotaFor(modelId.value);
  if (!quotaEntry) return "";
  const lines: string[] = [
    `QGenie · ${modelId.value.split("::").pop() ?? modelId.value}`,
    "",
  ];
  for (const traffic of CLASSES) {
    const bucket = quotaEntry[traffic];
    const marker =
      traffic === activeClass.value ? `  ← ${t("qgenieQuota.inUse")}` : "";
    lines.push(`${t(`qgenieQuota.class.${traffic}`)}${marker}`);
    lines.push(`  ${t("qgenieQuota.daily")}  ${formatWindow(bucket.day)}`);
    if (bucket.week) {
      lines.push(`  ${t("qgenieQuota.weekly")} ${formatWindow(bucket.week)}`);
    }
  }
  const cost = qgenie.cost;
  if (cost) {
    lines.push("", t("qgenieQuota.costHeading"));
    lines.push(
      `  ${t("qgenieQuota.costToday")} ${formatUsd(cost.day_usd)}${
        cost.day_cap_usd ? ` / ${formatUsd(cost.day_cap_usd)}` : ""
      }`,
    );
    lines.push(
      `  ${t("qgenieQuota.costMonth")} ${formatUsd(cost.month_usd)}${
        cost.month_cap_usd ? ` / ${formatUsd(cost.month_cap_usd)}` : ""
      }`,
    );
  }
  const fetchedAt = qgenie.payload?.fetched_at;
  if (typeof fetchedAt === "string" && fetchedAt !== "") {
    const parsed = new Date(fetchedAt);
    if (!Number.isNaN(parsed.getTime())) {
      const when = parsed.toLocaleTimeString(locale.value);
      lines.push(
        "",
        qgenie.stale
          ? t("qgenieQuota.staleAt", { when })
          : t("qgenieQuota.fetchedAt", { when }),
      );
    }
  }
  return lines.join("\n");
});
</script>

<template>
  <!-- qai-service: one pool, one bar (unchanged shape). -->
  <div
    v-if="showQaiService"
    class="sidebar-quota"
    :title="tooltip"
    data-testid="sidebar-quota-gauge"
  >
    <div class="sidebar-quota-head">
      <span class="sidebar-quota-label">{{ t("quota.label") }}</span>
      <span class="sidebar-quota-nums">
        <span data-testid="sidebar-quota-remaining">{{ remainingText }}</span>
        <span class="sidebar-quota-sep">/</span>
        <span>{{ allocatedText }}</span>
      </span>
    </div>
    <div
      class="sidebar-quota-bar"
      role="progressbar"
      :aria-label="t('quota.label')"
      :aria-valuenow="Math.round(quota.remainingPercent)"
      aria-valuemin="0"
      aria-valuemax="100"
    >
      <div
        class="sidebar-quota-fill"
        :class="`is-${severity}`"
        :style="{ width: `${quota.remainingPercent}%` }"
      />
    </div>
  </div>

  <!-- QGenie: one bar split into two independent halves.
       A button, not a div: the whole gauge opens the per-model breakdown, and
       that has to be reachable by keyboard like any other control. -->
  <button
    v-else-if="showQGenie"
    type="button"
    class="sidebar-quota sidebar-quota--clickable"
    :title="qgenieTooltip"
    :aria-label="t('qgenieQuota.dialogTitle')"
    data-testid="sidebar-qgenie-gauge"
    @click="dialogOpen = true"
  >
    <div class="sidebar-quota-head">
      <span class="sidebar-quota-label">{{ t("qgenieQuota.label") }}</span>
      <span v-if="qgenie.stale" class="sidebar-quota-stale">
        {{ t("qgenieQuota.staleBadge") }}
      </span>
    </div>
    <div class="sidebar-qgenie-split">
      <div
        v-for="half in qgenieHalves"
        :key="half.traffic"
        class="sidebar-qgenie-half"
        :class="{ 'is-active': half.active }"
        :data-testid="`sidebar-qgenie-half-${half.traffic}`"
      >
        <div
          class="sidebar-quota-bar"
          role="progressbar"
          :aria-label="half.label"
          :aria-valuenow="Math.round(half.usedPercent)"
          aria-valuemin="0"
          aria-valuemax="100"
        >
          <div
            class="sidebar-quota-fill"
            :class="`is-${half.severity}`"
            :style="{ width: `${half.usedPercent}%` }"
          />
        </div>
        <span class="sidebar-qgenie-caption">
          <span class="sidebar-qgenie-class">{{ half.label }}</span>
          <span class="sidebar-qgenie-pct">
            {{ half.exhausted ? t("qgenieQuota.full") : `${half.usedPercent.toFixed(1)}%` }}
          </span>
        </span>
      </div>
    </div>
  </button>

  <!-- Per-model daily breakdown. Mounted alongside the gauge (not inside the
       button) so its own Teleport controls placement. -->
  <QGenieQuotaDialog
    v-if="IS_INTERNAL"
    :visible="dialogOpen"
    :selected-model-id="modelId"
    :decision="decision"
    @close="closeDialog"
    @keep-going="onKeepGoing"
    @switch-to="onSwitchTo"
    @switch-model="onSwitchModel"
  />
</template>

<style scoped>
.sidebar-quota {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  /* Sits inside .sidebar-footer, which already owns the top border + padding;
     only a little breathing room from the control row below is needed. */
  padding-bottom: var(--space-1);
}

/* The QGenie gauge is a <button> (it opens the per-model breakdown), so the
   native button chrome is reset back to the plain block the qai-service gauge
   renders as — only the affordances (cursor, hover, focus ring) are added. */
.sidebar-quota--clickable {
  appearance: none;
  width: 100%;
  border: none;
  background: none;
  padding: 0 0 var(--space-1);
  font: inherit;
  color: inherit;
  text-align: left;
  cursor: pointer;
  border-radius: var(--radius-sm, 6px);
}

.sidebar-quota--clickable:hover .sidebar-quota-label {
  color: var(--text-primary);
}

.sidebar-quota--clickable:focus-visible {
  outline: 2px solid var(--accent, var(--text-secondary));
  outline-offset: 2px;
}

.sidebar-quota-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2);
}

.sidebar-quota-label {
  font-size: 0.68rem;
  font-weight: 650;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  color: var(--text-secondary);
  white-space: nowrap;
}

.sidebar-quota-nums {
  font-family: var(--font-mono);
  font-size: 0.7rem;
  color: var(--text-secondary);
  white-space: nowrap;
}

.sidebar-quota-sep {
  margin: 0 2px;
  color: var(--text-muted);
}

.sidebar-quota-bar {
  height: 4px;
  border-radius: 2px;
  background: var(--bg-tertiary);
  overflow: hidden;
}

.sidebar-quota-fill {
  height: 100%;
  border-radius: 2px;
  background: var(--success);
  /* Width animates as each answer spends tokens; colour animates on a
     threshold crossing so the change reads as deliberate, not a flicker. */
  transition: width 0.3s ease, background-color 0.3s ease;
}

.sidebar-quota-fill.is-low {
  background: var(--warning);
}

.sidebar-quota-fill.is-critical {
  background: var(--error);
}

/* ── QGenie split bar ─────────────────────────────────────────────────────
   Two halves side by side, each its own 0-100% scale. A shared scale would
   misrepresent the data: the two allowances are independent and often differ
   in size (measured up to 5x), so "40% + 60%" would sum to nothing real. */
.sidebar-qgenie-split {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-2);
}

.sidebar-qgenie-half {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  /* The side NOT currently billed is dimmed rather than hidden: the user still
     needs to see its headroom to judge whether switching is worth it. */
  opacity: 0.45;
  transition: opacity 0.2s ease;
}

.sidebar-qgenie-half.is-active {
  opacity: 1;
}

.sidebar-qgenie-caption {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-1);
  min-width: 0;
}

.sidebar-qgenie-class {
  font-size: 0.6rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sidebar-qgenie-pct {
  font-family: var(--font-mono);
  font-size: 0.6rem;
  color: var(--text-muted);
  white-space: nowrap;
}

.sidebar-qgenie-half.is-active .sidebar-qgenie-class,
.sidebar-qgenie-half.is-active .sidebar-qgenie-pct {
  color: var(--text-secondary);
}

.sidebar-quota-stale {
  font-size: 0.6rem;
  color: var(--warning);
  white-space: nowrap;
}

@media (prefers-reduced-motion: reduce) {
  .sidebar-qgenie-half {
    transition: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .sidebar-quota-fill {
    transition: none;
  }
}
</style>
