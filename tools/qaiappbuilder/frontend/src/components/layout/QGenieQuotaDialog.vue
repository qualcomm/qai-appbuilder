<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * QGenieQuotaDialog — full per-model breakdown of the QGenie daily allowances.
 *
 * Opened by clicking the sidebar quota gauge. The gauge's tooltip can only
 * describe the SELECTED model (a hover string has no room for more), but the
 * account's allowances are per-model and a user planning a long session needs
 * to see which models still have headroom — that is what this table is for.
 *
 * Shows, for every model the upstream reports usage on: today's used / total
 * for BOTH traffic classes, which class is currently billed, and the account's
 * total spend. Models with no usage today are collapsed behind a toggle so the
 * common case stays short (the upstream reports ~40 models, typically only a
 * handful of which have been touched).
 *
 * Structurally a sibling of BudgetDecisionDialog / RenameDialog: reuses the
 * global `.rename-dialog*` classes + focus trap + Escape/backdrop guard.
 * AGENTS.md §3.9.2: project-defined dialog — native confirm/alert are
 * forbidden.
 */
import { computed, ref, toRef } from "vue";
import { useI18n } from "vue-i18n";

import { useFocusTrap } from "@/composables/useFocusTrap";
import { fmtTokenCount } from "@/utils/contextBadge";
import {
  type QGenieBucket,
  type TrafficClass,
  useQGenieQuotaStore,
} from "@/stores/qgenieQuota";

interface Props {
  visible: boolean;
  /** Model whose row is highlighted — the one currently selected in chat. */
  selectedModelId: string;
  /**
   * Present when opened by an escalating quota warning rather than by clicking
   * the gauge. Adds a banner and the decision buttons; the table below is the
   * same either way, because the numbers the user needs to decide with ARE the
   * per-model breakdown.
   */
  decision?: {
    stage: "warned" | "final";
    active: TrafficClass;
    other: TrafficClass;
    activeRemaining: number | null;
    otherRemaining: number | null;
    activePercent: number;
    otherPercent: number;
    /** False when the other side is spent too — switching would not help. */
    switchable: boolean;
  } | null;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  close: [];
  /** Bill subsequent requests for the SELECTED model to this class. */
  switchTo: [traffic: TrafficClass];
  /** Bill subsequent requests for an arbitrary model to this class. */
  switchModel: [modelId: string, traffic: TrafficClass];
  /** Dismiss the warning and keep spending the remaining allowance. */
  keepGoing: [];
}>();

const { t, locale } = useI18n();
const qgenie = useQGenieQuotaStore();
const dialogEl = ref<HTMLElement | null>(null);

useFocusTrap(dialogEl, { active: toRef(props, "visible"), focusFirst: true });

/** Models with no usage today are hidden until asked for. */
const showIdle = ref(false);

const CLASSES: readonly TrafficClass[] = ["Api", "UI"];

/**
 * Compact token count — `694K`, `13.6M`.
 *
 * Reuses the project's single token formatter rather than grouped digits:
 * `123,917 / 13,600,000` makes the reader count commas to compare two figures,
 * which is the one thing this table exists to make easy. Same function the
 * composer context badge uses, so a count never renders two ways.
 */
function formatTokens(value: number): string {
  return fmtTokenCount(value);
}

function formatUsd(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `$${value.toFixed(2)}`;
}

interface ClassCell {
  traffic: TrafficClass;
  label: string;
  /** Null when the upstream did not report a daily window for this class. */
  used: number | null;
  limit: number | null;
  percent: number | null;
  exhausted: boolean;
  active: boolean;
}

interface ModelRow {
  modelId: string;
  shortName: string;
  selected: boolean;
  touchedToday: boolean;
  cells: ClassCell[];
  /**
   * The bucket this model is NOT currently billed to, or `null`.
   *
   * Precomputed because clicking the model name means "move it to the other
   * side", and that target must not be recomputed in the template on every
   * render. `null` only if the active class is somehow absent from `cells`,
   * in which case the name is simply not clickable.
   */
  otherCell: ClassCell | null;
}

function dailyOf(bucket: QGenieBucket | undefined): {
  used: number | null;
  limit: number | null;
} {
  const day = bucket?.day;
  if (!day) return { used: null, limit: null };
  return { used: day.used, limit: day.limit };
}

/**
 * One row per model, ordered: selected first, then models used today, then the
 * rest alphabetically — so the two rows a user came to check are always on top.
 */
const rows = computed<ModelRow[]>(() => {
  const models = qgenie.payload?.models ?? {};
  const selectedBare = props.selectedModelId.split("::").pop() ?? "";

  // The upstream reports allowances for every model the ACCOUNT is entitled to,
  // but this install only offers the provider config's `models[]` — the same
  // list the main model dropdown is built from. Offering a traffic-class switch
  // for a model the user cannot select is a dead end, so the extras are
  // dropped. An empty set means "not known yet" and must NOT filter, or a
  // failed side-fetch would blank a table whose own data arrived fine.
  const allowed = new Set(qgenie.selectableModelIds);
  const ids = Object.keys(models).filter(
    (id) => allowed.size === 0 || allowed.has(id),
  );

  const built = ids.map((modelId) => {
    const quota = models[modelId];
    const activeClass = qgenie.classFor(modelId);
    const bare = modelId.split("::").pop() ?? modelId;
    const cells = CLASSES.map((traffic) => {
      const { used, limit } = dailyOf(quota?.[traffic]);
      return {
        traffic,
        label: t(`qgenieQuota.class.${traffic}`),
        used,
        limit,
        percent:
          used !== null && limit !== null && limit > 0
            ? (used / limit) * 100
            : null,
        exhausted: limit !== null && (limit <= 0 || used === limit),
        active: traffic === activeClass,
      };
    });
    return {
      modelId,
      shortName: bare,
      selected: bare === selectedBare,
      touchedToday: cells.some((c) => (c.used ?? 0) > 0),
      cells,
      otherCell: cells.find((c) => !c.active) ?? null,
    };
  });

  // Same order as the main model picker.
  //
  // `selectableModelIds` preserves the provider config's `models[]` sequence —
  // exactly what `useOcModels.availableModels` builds the dropdown from. A user
  // who knows their models by position in that list should find them here in
  // the same places; re-sorting alphabetically (or by usage) meant reading the
  // table was a fresh search every time.
  //
  // Models the upstream reports but the config does not list fall back to
  // alphabetical, after the known ones.
  const order = new Map(
    qgenie.selectableModelIds.map((id, index) => [id, index]),
  );
  const rank = (row: ModelRow): number =>
    order.get(row.modelId) ?? Number.MAX_SAFE_INTEGER;

  return built.sort((a, b) => {
    const ra = rank(a);
    const rb = rank(b);
    if (ra !== rb) return ra - rb;
    return a.shortName.localeCompare(b.shortName);
  });
});

/**
 * Substring filter over the model name.
 *
 * Case-insensitive and matched against the bare name (what the row shows), not
 * the `provider::model` id — a user typing "haiku" is reading the table, not
 * the wire format.
 */
const filterText = ref("");

const visibleRows = computed(() => {
  const byUsage = showIdle.value
    ? rows.value
    : rows.value.filter((r) => r.touchedToday || r.selected);
  const needle = filterText.value.trim().toLowerCase();
  if (needle === "") return byUsage;
  return byUsage.filter((r) => r.shortName.toLowerCase().includes(needle));
});

/**
 * Count of rows the idle toggle is hiding.
 *
 * Measured against the SAME filter as the table, so the toggle never offers to
 * reveal models the current query excludes anyway.
 */
const idleCount = computed(() => {
  const needle = filterText.value.trim().toLowerCase();
  const matching =
    needle === ""
      ? rows.value
      : rows.value.filter((r) => r.shortName.toLowerCase().includes(needle));
  return matching.length - visibleRows.value.length;
});

/**
 * Pull a fresh reading on demand.
 *
 * `force` bypasses the backend cooldown: the user clicking sync has just done
 * something they expect to see reflected, and serving them the cached answer
 * would look broken. QGenie throttles these endpoints at ~60 s, so a rejected
 * read surfaces as the `rate_limited` reason rather than silence.
 */
async function onSync(): Promise<void> {
  await qgenie.refresh(true);
}

const cost = computed(() => qgenie.cost);

const fetchedAtText = computed(() => {
  const raw = qgenie.payload?.fetched_at;
  if (typeof raw !== "string" || raw === "") return "";
  const parsed = new Date(raw);
  if (Number.isNaN(parsed.getTime())) return "";
  const when = parsed.toLocaleTimeString(locale.value);
  return qgenie.stale
    ? t("qgenieQuota.staleAt", { when })
    : t("qgenieQuota.fetchedAt", { when });
});

/** Title switches to the warning wording when opened by a threshold crossing. */
const decisionTitle = computed(() => {
  const d = props.decision;
  if (!d) return t("qgenieQuota.dialogTitle");
  if (!d.switchable) return t("qgenieQuota.bothTitle");
  return d.stage === "final"
    ? t("qgenieQuota.finalTitle", { class: t(`qgenieQuota.class.${d.active}`) })
    : t("qgenieQuota.switchTitle", { class: t(`qgenieQuota.class.${d.active}`) });
});

const decisionBody = computed(() => {
  const d = props.decision;
  if (!d) return "";
  const shortName =
    props.selectedModelId.split("::").pop() ?? props.selectedModelId;
  if (!d.switchable) {
    return t("qgenieQuota.bothBody", { model: shortName });
  }
  return d.stage === "final"
    ? t("qgenieQuota.finalBody", {
        model: shortName,
        class: t(`qgenieQuota.class.${d.active}`),
      })
    : t("qgenieQuota.switchBody", {
        model: shortName,
        class: t(`qgenieQuota.class.${d.active}`),
        other: t(`qgenieQuota.class.${d.other}`),
        percent: d.activePercent.toFixed(1),
      });
});

/**
 * Human-readable reason the figures are missing or old, or "".
 *
 * A vanished / stale gauge on its own tells the user nothing actionable — "no
 * key" and "throttled for another minute" look identical. Known codes get a
 * sentence naming the fix; an unknown one still shows the raw code rather than
 * being swallowed.
 */
const reasonText = computed(() => {
  const code = qgenie.errorReason;
  if (code === null || code === "") return "";
  const known = [
    "no_api_key",
    "rate_limited",
    "unreachable",
    "not_configured",
    "bad_base_url",
  ];
  return known.includes(code)
    ? t(`qgenieQuota.reason.${code}`)
    : t("qgenieQuota.reason.unknown", { code });
});

/** Remaining tokens, or a dash when the upstream did not report the window. */
function remainingText(value: number | null): string {
  if (value === null) return "—";
  return t("qgenieQuota.remainingTokens", { count: formatTokens(value) });
}

/**
 * Set a model's quota class by clicking its row.
 *
 * Works for ANY model, not just the selected one: the preference is stored per
 * model on the provider config and read per request, so pre-setting a model you
 * are about to switch to — or one a sub-agent runs on, which is otherwise
 * unreachable from the UI — takes effect the moment it is used.
 *
 * Clicking the row that is already active is a no-op rather than a toggle: a
 * mis-click must never silently move a model's billing.
 */
function onClassRowClick(
  row: ModelRow,
  cell: ClassCell,
  event?: MouseEvent,
): void {
  // A click inside the name cell belongs to the name, not to the row it happens
  // to sit in. Ownership is decided HERE rather than with `.stop` on the cell:
  // the row's listener is on the ancestor, so the cell would have to suppress an
  // event the row is equally entitled to — and when the name cell's row was the
  // inactive one, BOTH fired and a single click switched the model twice.
  //
  // Duck-typed rather than `instanceof Element`: under jsdom the event target
  // can come from a different realm, where `instanceof` is false and the guard
  // would silently do nothing.
  const target = event?.target as { closest?: (s: string) => unknown } | null;
  if (typeof target?.closest === "function") {
    if (target.closest(".qgenie-name-cell") !== null) return;
  }
  if (cell.active) return;
  emit("switchModel", row.modelId, cell.traffic);
}

/**
 * Clicking the model name moves it to the bucket it is not on.
 *
 * Separate from the row handler on purpose: the name cell physically sits in
 * the API/SDK row, so sharing that row's handler made the name inert whenever
 * API/SDK was the active side. With two buckets there is only one place a
 * "switch this model" click can go, and the cell's tooltip names it.
 */
function onNameClick(row: ModelRow): void {
  const target = row.otherCell;
  if (target === null) return;
  emit("switchModel", row.modelId, target.traffic);
}

// Backdrop-dismiss guard (RenameDialog.vue parity): a dismiss must both start
// AND end on the overlay so a drag-select never spuriously closes it.
const pointerDownOnOverlay = ref(false);

function onOverlayPointerDown(event: PointerEvent): void {
  pointerDownOnOverlay.value = event.target === event.currentTarget;
}

function onOverlayClick(event: MouseEvent): void {
  if (event.target === event.currentTarget && pointerDownOnOverlay.value) {
    emit("close");
  }
  pointerDownOnOverlay.value = false;
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="rename-dialog-overlay"
      data-testid="qgenie-quota-overlay"
      @pointerdown="onOverlayPointerDown"
      @click="onOverlayClick"
    >
      <div
        ref="dialogEl"
        class="rename-dialog qgenie-quota-dialog"
        role="dialog"
        aria-modal="true"
        :aria-label="t('qgenieQuota.dialogTitle')"
        data-testid="qgenie-quota-dialog"
        @keydown.esc="emit('close')"
      >
        <!-- Title row carries the dismiss, matching FavoritesDialog: a
             list-style dialog is scrolled and skimmed, so the way out should sit
             in a fixed corner rather than below content whose height varies. -->
        <div class="qgenie-title-row">
          <span class="rename-dialog-title qgenie-title-text">
            {{ decisionTitle }}
          </span>
          <button
            type="button"
            class="btn btn-ghost btn-xs qgenie-close-x"
            :title="t('qgenieQuota.close')"
            :aria-label="t('qgenieQuota.close')"
            data-testid="qgenie-quota-close"
            @click="emit('close')"
          >
            ✕
          </button>
        </div>

        <!-- Warning banner: only when opened BY a threshold crossing. Lists
             both sides' headroom side by side, because whether a switch is
             worth making is the user's call — "96% vs 94%" may be worth it to
             one person and pointless to another, and code guessing that would
             either nag or stay wrongly silent. -->
        <div
          v-if="decision"
          class="qgenie-decision"
          :class="`is-${decision.stage}`"
          data-testid="qgenie-quota-decision"
        >
          <div class="qgenie-decision-body">{{ decisionBody }}</div>
          <div class="qgenie-decision-sides">
            <div class="qgenie-decision-side is-active">
              <span class="qgenie-decision-side-label">
                {{ t(`qgenieQuota.class.${decision.active}`) }}
                <span class="qgenie-decision-inuse">
                  {{ t("qgenieQuota.inUse") }}
                </span>
              </span>
              <span class="qgenie-decision-side-figure">
                {{ remainingText(decision.activeRemaining) }}
              </span>
            </div>
            <div class="qgenie-decision-side">
              <span class="qgenie-decision-side-label">
                {{ t(`qgenieQuota.class.${decision.other}`) }}
                <span v-if="!decision.switchable" class="qgenie-exhausted">
                  {{ t("qgenieQuota.full") }}
                </span>
              </span>
              <span class="qgenie-decision-side-figure">
                {{ remainingText(decision.otherRemaining) }}
              </span>
            </div>
          </div>
        </div>

        <!-- Account spend. Labelled as the all-models total because the
             upstream cannot attribute it per model. -->
        <div v-if="cost" class="qgenie-cost">
          <div class="qgenie-cost-heading">
            {{ t("qgenieQuota.costHeading") }}
          </div>
          <div class="qgenie-cost-figures">
            <span>
              {{ t("qgenieQuota.costToday") }}
              <strong>{{ formatUsd(cost.day_usd) }}</strong>
              <span v-if="cost.day_cap_usd" class="qgenie-cost-cap">
                / {{ formatUsd(cost.day_cap_usd) }}
              </span>
            </span>
            <span>
              {{ t("qgenieQuota.costMonth") }}
              <strong>{{ formatUsd(cost.month_usd) }}</strong>
              <span v-if="cost.month_cap_usd" class="qgenie-cost-cap">
                / {{ formatUsd(cost.month_cap_usd) }}
              </span>
            </span>
            <span v-if="cost.tier" class="qgenie-cost-tier">
              {{ cost.tier }}
            </span>
          </div>
        </div>

        <!-- Filter box: the account is entitled to dozens of models, and
             scanning for one by eye is the slow way. Sits above the scroll
             region so it stays put while the list moves. -->
        <div class="qgenie-filter-row">
          <input
            v-model="filterText"
            type="search"
            class="qgenie-filter-input"
            :placeholder="t('qgenieQuota.filterPlaceholder')"
            :aria-label="t('qgenieQuota.filterPlaceholder')"
            data-testid="qgenie-quota-filter"
          />
        </div>

        <div class="qgenie-table-scroll">
          <table class="qgenie-table">
            <thead>
              <tr>
                <th scope="col">{{ t("qgenieQuota.colModel") }}</th>
                <th scope="col">{{ t("qgenieQuota.colClass") }}</th>
                <th scope="col" class="is-num">
                  {{ t("qgenieQuota.colUsedToday") }}
                </th>
                <th scope="col" class="is-num">
                  {{ t("qgenieQuota.colDailyLimit") }}
                </th>
              </tr>
            </thead>
            <tbody>
              <template v-for="row in visibleRows" :key="row.modelId">
                <tr
                  v-for="(cell, index) in row.cells"
                  :key="`${row.modelId}-${cell.traffic}`"
                  :class="{
                    'is-selected': row.selected,
                    'is-active-class': cell.active,
                    'is-group-start': index === 0,
                    'is-pickable': !cell.active,
                  }"
                  :title="
                    cell.active
                      ? t('qgenieQuota.inUse')
                      : t('qgenieQuota.clickToSwitch', { class: cell.label })
                  "
                  :data-testid="
                    cell.active
                      ? undefined
                      : `qgenie-quota-pick-${row.shortName}-${cell.traffic}`
                  "
                  @click="onClassRowClick(row, cell, $event)"
                >
                  <!-- The name cell handles its OWN click and stops it there.

                       The name has to live inside one of the two class rows (a
                       table cell belongs to a row), and that row is the API/SDK
                       one. Leaving the click to bubble meant it inherited that
                       row's "already active, do nothing" guard — the reported
                       bug: clickable under IDE/CLI, dead under API/SDK.

                       With only two buckets, "click the model" has exactly one
                       sensible meaning: move it to the side it is not on. The
                       tooltip names that side, so nothing is implicit. -->
                  <th
                    scope="row"
                    class="qgenie-name-cell"
                    :class="{ 'is-pickable': index === 0 && row.otherCell !== null }"
                    :title="
                      index === 0 && row.otherCell !== null
                        ? t('qgenieQuota.clickToSwitch', {
                            class: row.otherCell.label,
                          })
                        : undefined
                    "
                    :data-testid="
                      index === 0 ? `qgenie-quota-pick-name-${row.shortName}` : undefined
                    "
                    @click="onNameClick(row)"
                  >
                    <template v-if="index === 0">
                      <span class="qgenie-model-name">{{ row.shortName }}</span>
                      <span v-if="row.selected" class="qgenie-model-badge">
                        {{ t("qgenieQuota.selected") }}
                      </span>
                    </template>
                  </th>
                  <td class="qgenie-class-cell">
                    <span class="qgenie-class-label">{{ cell.label }}</span>
                    <span
                      v-if="cell.active"
                      class="qgenie-inuse-dot"
                      :title="t('qgenieQuota.inUse')"
                    />
                  </td>
                  <td class="is-num">
                    <template v-if="cell.used !== null">
                      {{ formatTokens(cell.used) }}
                      <span v-if="cell.percent !== null" class="qgenie-pct-inline">
                        ({{ cell.percent.toFixed(2) }}%)
                      </span>
                    </template>
                    <span v-else class="qgenie-unknown">
                      {{ t("qgenieQuota.unknown") }}
                    </span>
                  </td>
                  <td class="is-num">
                    <template v-if="cell.limit !== null">
                      {{ formatTokens(cell.limit) }}
                      <span v-if="cell.exhausted" class="qgenie-exhausted">
                        {{ t("qgenieQuota.full") }}
                      </span>
                    </template>
                    <span v-else class="qgenie-unknown">
                      {{ t("qgenieQuota.unknown") }}
                    </span>
                  </td>
                </tr>
              </template>
              <tr v-if="visibleRows.length === 0">
                <td colspan="4" class="qgenie-empty">
                  {{ t("qgenieQuota.noMatch", { query: filterText }) }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <button
          v-if="idleCount > 0 || showIdle"
          type="button"
          class="qgenie-toggle-idle"
          data-testid="qgenie-quota-toggle-idle"
          @click="showIdle = !showIdle"
        >
          {{
            showIdle
              ? t("qgenieQuota.hideIdle")
              : t("qgenieQuota.showIdle", { count: idleCount })
          }}
        </button>

        <div v-if="reasonText" class="qgenie-reason" data-testid="qgenie-quota-reason">
          {{ reasonText }}
        </div>

        <!-- Data age + manual sync together: the timestamp is exactly what
             prompts "is this current?", and the answer should be one click away
             rather than "wait for the next turn to end". -->
        <div class="qgenie-footer-row">
          <span v-if="fetchedAtText" class="qgenie-fetched-at">
            {{ fetchedAtText }}
          </span>
          <button
            type="button"
            class="qgenie-sync-btn"
            :disabled="qgenie.loading"
            :title="t('qgenieQuota.syncTooltip')"
            data-testid="qgenie-quota-sync"
            @click="onSync"
          >
            <span
              class="qgenie-sync-icon"
              :class="{ 'is-spinning': qgenie.loading }"
              aria-hidden="true"
              >⟳</span
            >
            {{ qgenie.loading ? t("qgenieQuota.syncing") : t("qgenieQuota.sync") }}
          </button>
        </div>

        <!-- Decision mode gets three exits, matching the three real choices:
             move to the other bucket, spend what is left here, or drop this
             model. Browsing mode just closes. -->
        <div v-if="decision" class="rename-dialog-actions qgenie-decision-actions">
          <button
            type="button"
            class="rename-dialog-btn rename-dialog-btn--cancel"
            data-testid="qgenie-quota-pick-model"
            @click="emit('close')"
          >
            {{ t("qgenieQuota.pickAnotherModel") }}
          </button>
          <button
            type="button"
            class="rename-dialog-btn rename-dialog-btn--cancel"
            data-testid="qgenie-quota-keep-going"
            @click="emit('keepGoing')"
          >
            {{ t("qgenieQuota.keepGoing") }}
          </button>
          <button
            v-if="decision.switchable"
            type="button"
            class="rename-dialog-btn rename-dialog-btn--confirm"
            data-testid="qgenie-quota-switch"
            @click="emit('switchTo', decision.other)"
          >
            {{
              t("qgenieQuota.switchConfirm", {
                other: t(`qgenieQuota.class.${decision.other}`),
              })
            }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* Reuses the global `.rename-dialog*` tokens (overlay / dialog / title /
   actions / btn); only the table + spend block are local, all from theme vars
   so light/dark follow the app. */
/* Wider than the old 620px: at that width the "in use" dot wrapped onto its
   own line after the class label, which read as a stray bullet. */
.qgenie-quota-dialog {
  max-width: 760px;
  width: min(760px, calc(100vw - 32px));
}

/* Title + dismiss on one line (FavoritesDialog parity). The old bottom button
   used the shared `.rename-dialog-btn`, whose `flex: 1` only makes sense beside
   siblings — alone in its row it collapsed to a small accent block. */
.qgenie-title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.qgenie-title-text {
  flex: 1 1 auto;
  min-width: 0;
}

.qgenie-close-x {
  flex: none;
  line-height: 1;
  color: var(--text-muted);
}

.qgenie-close-x:hover {
  color: var(--text-primary);
}

.qgenie-cost {
  padding: 8px 10px;
  margin-bottom: 10px;
  border: 1px solid var(--border-light);
  border-radius: var(--radius-sm, 6px);
  background: var(--bg-primary, transparent);
}

.qgenie-cost-heading {
  font-size: var(--text-xs);
  color: var(--text-muted);
  margin-bottom: 4px;
}

.qgenie-cost-figures {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  align-items: baseline;
  font-size: var(--text-sm);
  color: var(--text-secondary);
}

.qgenie-cost-figures strong {
  font-family: var(--font-mono);
  color: var(--text-primary);
}

.qgenie-cost-cap {
  color: var(--text-muted);
  font-family: var(--font-mono);
}

.qgenie-cost-tier {
  padding: 1px 7px;
  border-radius: 999px;
  border: 1px solid var(--border-light);
  font-size: var(--text-xs);
  color: var(--text-muted);
}

/* Taller than the old 380px: one row per model (rather than two) already
   halves the height per entry, and the cap is what decides how many models are
   visible without scrolling. Still viewport-relative so the actions row stays
   reachable on a short window. */
.qgenie-table-scroll {
  max-height: min(58vh, 520px);
  overflow-y: auto;
  border: 1px solid var(--border-light);
  border-radius: var(--radius-md, 8px);
  background: var(--bg-primary, transparent);
}

.qgenie-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-xs);
}

.qgenie-table thead th {
  position: sticky;
  top: 0;
  z-index: 1;
  text-align: left;
  font-weight: 650;
  color: var(--text-muted);
  background: var(--bg-secondary);
  padding: 6px 10px;
  border-bottom: 1px solid var(--border-light);
  white-space: nowrap;
}

.qgenie-table tbody th,
.qgenie-table tbody td {
  padding: 5px 10px;
  color: var(--text-secondary);
  vertical-align: middle;
}

.qgenie-table tbody th {
  text-align: left;
  font-weight: 500;
  color: var(--text-primary);
}

.qgenie-table tr.is-group-start > th,
.qgenie-table tr.is-group-start > td {
  border-top: 1px solid var(--border-light);
}

.qgenie-table tr.is-selected > th {
  color: var(--text-primary);
  font-weight: 650;
}

/* The class actually being billed reads at full strength; the other is dimmed
   but still legible — its headroom is what makes a switch worth considering. */
.qgenie-table tbody tr:not(.is-active-class) > td {
  opacity: 0.55;
}

/* Clickable rows need a visible affordance, or the switch stays undiscovered:
   the row looked like plain text before. */
.qgenie-table tbody tr.is-pickable {
  cursor: pointer;
}

.qgenie-table tbody tr.is-pickable:hover > th,
.qgenie-table tbody tr.is-pickable:hover > td {
  background: color-mix(in srgb, var(--accent) 10%, transparent);
  opacity: 1;
}

.qgenie-name-cell {
  white-space: nowrap;
}

.is-num {
  text-align: right;
  font-family: var(--font-mono);
  white-space: nowrap;
}

.qgenie-model-name {
  font-family: var(--font-mono);
}

.qgenie-model-badge {
  margin-left: 6px;
  padding: 0 6px;
  border-radius: 999px;
  border: 1px solid var(--accent, var(--border-light));
  color: var(--accent, var(--text-secondary));
  font-size: 0.6rem;
  white-space: nowrap;
}

/* Label + dot on one line: the dot wrapping to its own row was the reported
   stray-bullet artefact. */
.qgenie-class-cell {
  white-space: nowrap;
}

.qgenie-class-label {
  font-weight: 600;
}

.qgenie-inuse-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-left: 6px;
  border-radius: 50%;
  background: var(--success);
  vertical-align: middle;
}

.qgenie-pct-inline,
.qgenie-unknown {
  color: var(--text-muted);
}

.qgenie-exhausted {
  margin-left: 6px;
  color: var(--error);
}

.qgenie-empty {
  padding: 18px 10px;
  text-align: center;
  color: var(--text-muted);
}

/* ── Filter + sync row ───────────────────────────────────────────────────── */
.qgenie-filter-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.qgenie-filter-input {
  flex: 1 1 auto;
  min-width: 0;
  padding: 6px 10px;
  border: 1px solid var(--border-light);
  border-radius: var(--radius-sm, 6px);
  background: var(--bg-primary, transparent);
  color: var(--text-primary);
  font-size: var(--text-xs);
}

.qgenie-filter-input:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 25%, transparent);
}

.qgenie-sync-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex: none;
  padding: 6px 12px;
  border: 1px solid var(--border-light);
  border-radius: var(--radius-sm, 6px);
  background: var(--bg-primary, transparent);
  color: var(--text-secondary);
  font-size: var(--text-xs);
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}

.qgenie-sync-btn:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--text-primary);
}

.qgenie-sync-btn:disabled {
  cursor: progress;
  opacity: 0.7;
}

.qgenie-sync-icon {
  display: inline-block;
  font-size: 0.95em;
  line-height: 1;
}

.qgenie-sync-icon.is-spinning {
  animation: qgenie-spin 0.9s linear infinite;
}

@keyframes qgenie-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .qgenie-sync-icon.is-spinning {
    animation: none;
  }
}

.qgenie-exhausted {
  margin-left: 6px;
  color: var(--error);
}

.qgenie-toggle-idle {
  margin-top: 8px;
  padding: 0;
  border: none;
  background: none;
  color: var(--accent, var(--text-secondary));
  font-size: var(--text-xs);
  cursor: pointer;
}

.qgenie-toggle-idle:hover {
  text-decoration: underline;
}

.qgenie-footer-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 8px;
}

.qgenie-fetched-at {
  font-size: var(--text-xs);
  color: var(--text-muted);
}

/* Why the figures are missing or old. Amber rather than red: the numbers may
   still be usable, they are just not fresh. */
.qgenie-reason {
  margin-top: 8px;
  padding: 6px 8px;
  border-radius: var(--radius-sm, 6px);
  border: 1px solid var(--warning);
  font-size: var(--text-xs);
  color: var(--text-secondary);
  line-height: 1.5;
}

/* ── Decision banner (only when opened by a threshold crossing) ─────────── */
.qgenie-decision {
  padding: 8px 10px;
  margin-bottom: 10px;
  border-radius: var(--radius-sm, 6px);
  border: 1px solid var(--warning);
  background: var(--bg-primary, transparent);
}

/* The last-chance stage reads as an error: the next reply may actually break. */
.qgenie-decision.is-final {
  border-color: var(--error);
}

.qgenie-decision-body {
  font-size: var(--text-sm);
  color: var(--text-primary);
  line-height: 1.5;
  margin-bottom: 8px;
}

/* Both allowances side by side: the comparison IS the decision, so neither is
   buried behind a hover or a second click. */
.qgenie-decision-sides {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.qgenie-decision-side {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 8px;
  border-radius: var(--radius-sm, 6px);
  border: 1px solid var(--border-light);
  min-width: 0;
}

.qgenie-decision-side.is-active {
  border-color: var(--accent, var(--text-secondary));
}

.qgenie-decision-side-label {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-secondary);
}

.qgenie-decision-inuse {
  margin-left: 4px;
  font-weight: 400;
  color: var(--text-muted);
}

.qgenie-decision-side-figure {
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  color: var(--text-primary);
}

.qgenie-decision-actions {
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}

/* A clickable class row for the selected model (switch without waiting to be
   prompted, and the only way back to the previous bucket). */
.qgenie-table tbody tr.is-pickable {
  cursor: pointer;
}

.qgenie-table tbody tr.is-pickable:hover > td {
  opacity: 1;
  background: var(--bg-secondary);
}
</style>
