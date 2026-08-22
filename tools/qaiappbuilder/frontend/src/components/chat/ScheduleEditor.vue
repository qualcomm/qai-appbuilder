<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ScheduleEditor — friendly editor for a scheduled task's schedule string.
 *
 * The backend parser (`src/qai/platform/scheduling/schedule_parser.py`) accepts
 * six textual forms. Typing raw text works but nobody remembers cron, and the
 * two wall-clock forms (`daily HH:MM±ZZ:ZZ` / `weekly <dow> HH:MM±ZZ:ZZ`) plus
 * the interval `start_at` are effectively undiscoverable. So this component
 * offers five modes that each *generate* the text the parser already speaks:
 *
 *   once     → local ISO timestamp   `2026-08-02T07:30:00+08:00`   (ONCE)
 *   interval → `every 30m` / `every 2h` / `every 1d`  + optional `start_at`
 *   daily    → `daily 07:30+08:00`                                 (CRON)
 *   weekly   → `weekly mon 09:00+08:00`                            (CRON)
 *   advanced → the raw string, verbatim — cron and anything else, zero loss
 *
 * Design contract: this is a *lossless* editor over a string. `v-model` is the
 * pair the API takes (`schedule` + `start_at`), and an incoming string is
 * reverse-parsed into whichever mode produced it; anything unrecognised lands
 * in `advanced` with the text untouched. That is the regression guard — no
 * existing schedule can be silently rewritten by merely opening the editor.
 *
 * The UTC offset is always the browser's current one (`getTimezoneOffset`), so
 * the wall clock the user types is the wall clock they read on their own
 * screen; the backend converts to UTC via `tz_offset_minutes`.
 */
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { ElDatePicker, ElTimePicker } from "element-plus";
import "element-plus/es/components/date-picker/style/css";
import "element-plus/es/components/time-picker/style/css";

/** The two API fields this editor owns. */
export interface ScheduleDraft {
  schedule: string;
  start_at: string | null;
}

type Mode = "once" | "interval" | "daily" | "weekly" | "advanced";
type IntervalUnit = "m" | "h" | "d";

/** `{ hours, minutes }` — what vue-datepicker binds in `time-picker` mode. */
interface TimeParts {
  hours: number;
  minutes: number;
}

const props = defineProps<{ modelValue: ScheduleDraft }>();
const emit = defineEmits<{ "update:modelValue": [ScheduleDraft] }>();

const { t, locale } = useI18n();

const MODES: readonly Mode[] = [
  "once",
  "interval",
  "daily",
  "weekly",
  "advanced",
] as const;

const WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
type Weekday = (typeof WEEKDAYS)[number];

const UNITS: readonly IntervalUnit[] = ["m", "h", "d"] as const;
const UNIT_LABEL: Record<IntervalUnit, string> = {
  m: "unitMinutes",
  h: "unitHours",
  d: "unitDays",
};

// ── Offset helpers ──────────────────────────────────────────────────────────

/** Minutes east of UTC for the browser's current zone (+08:00 ⇒ 480). */
function localOffsetMinutes(): number {
  return -new Date().getTimezoneOffset();
}

/** Format an east-positive minute offset as `+08:00` / `-05:30`. */
function formatOffset(minutes: number): string {
  const sign = minutes < 0 ? "-" : "+";
  const abs = Math.abs(minutes);
  return `${sign}${pad2(Math.floor(abs / 60))}:${pad2(abs % 60)}`;
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

// ── Mode state ──────────────────────────────────────────────────────────────

const mode = ref<Mode>("once");
/** `once` — the full instant (date + time in one picker). */
const onceAt = ref<Date>(nextHour());
/** `interval` — count + unit, and an optional explicit first run. */
const intervalCount = ref<number>(2);
const intervalUnit = ref<IntervalUnit>("h");
const firstRunAt = ref<Date | null>(null);
/** `daily` / `weekly` — wall clock, plus the weekday for `weekly`. */
const timeOfDay = ref<TimeParts>({ hours: 9, minutes: 0 });
/** ElTimePicker binds a ``Date``; keep our internal ``{hours, minutes}`` as
 * the persisted shape (reverse-parse and `wallClock` both read it) and expose
 * a Date-shaped view for the widget only. The Date's date part is meaningless
 * — we only echo it back on write to preserve `time-of-day` semantics. */
const timeOfDayDate = computed<Date>({
  get() {
    const d = new Date();
    d.setHours(timeOfDay.value.hours, timeOfDay.value.minutes, 0, 0);
    return d;
  },
  set(next) {
    if (!(next instanceof Date) || Number.isNaN(next.getTime())) return;
    timeOfDay.value = { hours: next.getHours(), minutes: next.getMinutes() };
  },
});

/** Split ``onceAt`` (Date) into a date-only Date and a time-only Date for two
 * separate pickers side-by-side. ElDatePicker's ``datetime`` mode kept its
 * time selector inside its calendar popover, which produced a nested/floating
 * time menu that visually overflowed the panel and covered other form
 * fields; two independent controls avoid that entirely. Writes patch the
 * underlying Date on the corresponding axis so the other part stays intact. */
const onceAtDate = computed<Date>({
  get() {
    const d = new Date(onceAt.value);
    d.setHours(0, 0, 0, 0);
    return d;
  },
  set(next) {
    if (!(next instanceof Date) || Number.isNaN(next.getTime())) return;
    const combined = new Date(next);
    combined.setHours(
      onceAt.value.getHours(),
      onceAt.value.getMinutes(),
      0,
      0,
    );
    onceAt.value = combined;
  },
});
const onceAtTime = computed<Date>({
  get() {
    const d = new Date();
    d.setHours(
      onceAt.value.getHours(),
      onceAt.value.getMinutes(),
      0,
      0,
    );
    return d;
  },
  set(next) {
    if (!(next instanceof Date) || Number.isNaN(next.getTime())) return;
    const combined = new Date(onceAt.value);
    combined.setHours(next.getHours(), next.getMinutes(), 0, 0);
    onceAt.value = combined;
  },
});

/** Same split for the interval mode's optional first-run instant. ``null`` is
 * a legit "unset" value on ``firstRunAt`` — reading the date/time computeds
 * on ``null`` returns ``null`` so the picker shows its placeholder; writing
 * either half on top of ``null`` seeds the other half from now. */
const firstRunAtDate = computed<Date | null>({
  get() {
    if (firstRunAt.value === null) return null;
    const d = new Date(firstRunAt.value);
    d.setHours(0, 0, 0, 0);
    return d;
  },
  set(next) {
    if (next === null) {
      firstRunAt.value = null;
      return;
    }
    if (!(next instanceof Date) || Number.isNaN(next.getTime())) return;
    const base = firstRunAt.value ?? new Date();
    const combined = new Date(next);
    combined.setHours(base.getHours(), base.getMinutes(), 0, 0);
    firstRunAt.value = combined;
  },
});
const firstRunAtTime = computed<Date | null>({
  get() {
    if (firstRunAt.value === null) return null;
    const d = new Date();
    d.setHours(
      firstRunAt.value.getHours(),
      firstRunAt.value.getMinutes(),
      0,
      0,
    );
    return d;
  },
  set(next) {
    if (next === null) {
      firstRunAt.value = null;
      return;
    }
    if (!(next instanceof Date) || Number.isNaN(next.getTime())) return;
    const base = firstRunAt.value ?? new Date();
    const combined = new Date(base);
    combined.setHours(next.getHours(), next.getMinutes(), 0, 0);
    firstRunAt.value = combined;
  },
});
const weekday = ref<Weekday>("mon");
/** `advanced` — the raw string, passed through untouched. */
const rawText = ref<string>("");

/** A sensible default instant: the next whole hour, seconds zeroed. */
function nextHour(): Date {
  const d = new Date();
  d.setMinutes(0, 0, 0);
  d.setHours(d.getHours() + 1);
  return d;
}

// ── Generation (mode state → schedule string) ───────────────────────────────

/** Local ISO-8601 with explicit offset, e.g. `2026-08-02T07:30:00+08:00`. */
function toLocalIso(d: Date): string {
  return (
    `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}` +
    `T${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}` +
    formatOffset(localOffsetMinutes())
  );
}

/** The schedule string the current mode state denotes. */
const generated = computed<string>(() => {
  switch (mode.value) {
    case "once":
      return toLocalIso(onceAt.value);
    case "interval":
      return `every ${Math.max(1, Math.trunc(intervalCount.value || 1))}${intervalUnit.value}`;
    case "daily":
      return `daily ${wallClock()}`;
    case "weekly":
      return `weekly ${weekday.value} ${wallClock()}`;
    case "advanced":
      return rawText.value;
  }
});

/** `HH:MM+08:00` from the time-of-day picker plus the browser offset. */
function wallClock(): string {
  const { hours, minutes } = timeOfDay.value;
  return `${pad2(hours)}:${pad2(minutes)}${formatOffset(localOffsetMinutes())}`;
}

/** `start_at` only means something for `interval` (the other modes encode it). */
const generatedStartAt = computed<string | null>(() =>
  mode.value === "interval" && firstRunAt.value !== null
    ? toLocalIso(firstRunAt.value)
    : null,
);

// ── Reverse parse (schedule string → mode state) ────────────────────────────

const DAILY_RE = /^daily\s+(\d{1,2}):(\d{2})/i;
const WEEKLY_RE = /^weekly\s+([A-Za-z]{3,9}|[0-7])\s+(\d{1,2}):(\d{2})/i;
const EVERY_RE = /^every\s+(\d+)\s*([smhd])\s*$/i;
const ISO_RE = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/;

/** Backend day tokens → our seven-key weekday list (`7` folds to Sunday). */
const DOW_LOOKUP: Record<string, Weekday> = {
  "0": "sun", "7": "sun", sun: "sun", sunday: "sun",
  "1": "mon", mon: "mon", monday: "mon",
  "2": "tue", tue: "tue", tues: "tue", tuesday: "tue",
  "3": "wed", wed: "wed", weds: "wed", wednesday: "wed",
  "4": "thu", thu: "thu", thur: "thu", thurs: "thu", thursday: "thu",
  "5": "fri", fri: "fri", friday: "fri",
  "6": "sat", sat: "sat", saturday: "sat",
};

/**
 * Seed the mode state from an existing schedule string.
 *
 * Recognises exactly the forms this editor generates; everything else — cron,
 * bare durations, and any future syntax — goes to `advanced` with the string
 * preserved byte-for-byte, so re-saving an untouched task is a no-op.
 */
function hydrate(schedule: string, startAt: string | null): void {
  const text = schedule.trim();

  const daily = DAILY_RE.exec(text);
  if (daily !== null) {
    mode.value = "daily";
    timeOfDay.value = {
      hours: Number(daily[1]),
      minutes: Number(daily[2]),
    };
    return;
  }

  const weekly = WEEKLY_RE.exec(text);
  if (weekly !== null) {
    const dow = DOW_LOOKUP[(weekly[1] ?? "").toLowerCase()];
    if (dow !== undefined) {
      mode.value = "weekly";
      weekday.value = dow;
      timeOfDay.value = {
        hours: Number(weekly[2]),
        minutes: Number(weekly[3]),
      };
      return;
    }
  }

  const every = EVERY_RE.exec(text);
  if (every !== null) {
    const unit = (every[2] ?? "h").toLowerCase();
    // `s` has no dropdown entry (the UI's floor is a minute) — show it as raw
    // text rather than silently rounding the user's interval away.
    if (unit === "m" || unit === "h" || unit === "d") {
      mode.value = "interval";
      intervalCount.value = Number(every[1]);
      intervalUnit.value = unit;
      const seeded = startAt === null ? null : new Date(startAt);
      firstRunAt.value =
        seeded !== null && !Number.isNaN(seeded.getTime()) ? seeded : null;
      return;
    }
  }

  if (ISO_RE.test(text)) {
    const parsed = new Date(text);
    if (!Number.isNaN(parsed.getTime())) {
      mode.value = "once";
      onceAt.value = parsed;
      return;
    }
  }

  mode.value = "advanced";
  rawText.value = schedule;
}

// Hydrate from the incoming value, and re-hydrate when the parent swaps to a
// different task (the panel keys one editor per open task, but the modal is
// reused, so the prop can change identity under us).
hydrate(props.modelValue.schedule, props.modelValue.start_at);

watch(
  () => props.modelValue.schedule,
  (next) => {
    if (next !== generated.value) {
      hydrate(next, props.modelValue.start_at);
    }
  },
);

// ── Emit ────────────────────────────────────────────────────────────────────

// One watcher over both derived values: any control change re-emits the pair.
watch(
  [generated, generatedStartAt],
  ([schedule, startAt]) => {
    if (
      schedule === props.modelValue.schedule &&
      startAt === props.modelValue.start_at
    ) {
      return;
    }
    emit("update:modelValue", { schedule, start_at: startAt });
  },
  { immediate: true },
);

/** Switch mode and immediately publish that mode's string. */
function selectMode(next: Mode): void {
  if (next === mode.value) return;
  // Entering advanced from a generated mode carries the text over, so the user
  // edits what they were just looking at instead of an empty box.
  if (next === "advanced" && rawText.value === "") {
    rawText.value = generated.value;
  }
  mode.value = next;
}
</script>

<template>
  <div class="config-field" data-testid="schedule-editor">
    <label class="config-label">{{ t("scheduledTasks.fieldSchedule") }}</label>

    <div class="sched-modes" role="group" :aria-label="t('scheduledTasks.scheduleMode')">
      <button
        v-for="m in MODES"
        :key="m"
        type="button"
        class="sched-mode"
        :class="{ 'sched-mode--on': mode === m }"
        :aria-pressed="mode === m"
        :data-testid="`schedule-mode-${m}`"
        @click="selectMode(m)"
      >
        {{ t(`scheduledTasks.mode${m.charAt(0).toUpperCase() + m.slice(1)}`) }}
      </button>
    </div>

    <!-- once: split into a date-only picker + a time-only picker so each has
         its own popover. ``ElDatePicker type=datetime`` kept its time menu
         inside the calendar popover, which floated over other form fields. -->
    <div v-if="mode === 'once'" class="sched-row-fields">
      <label class="sched-sublabel">
        {{ t("scheduledTasks.fieldDate") }}
        <ElDatePicker
          v-model="onceAtDate"
          type="date"
          format="YYYY-MM-DD"
          :clearable="false"
          :teleported="false"
          data-testid="schedule-once-date"
        />
      </label>
      <label class="sched-sublabel">
        {{ t("scheduledTasks.fieldTime") }}
        <ElTimePicker
          v-model="onceAtTime"
          format="HH:mm"
          :clearable="false"
          :teleported="false"
          data-testid="schedule-once-time"
        />
      </label>
    </div>

    <!-- interval: count + unit, plus an optional explicit first run -->
    <template v-else-if="mode === 'interval'">
      <div class="sched-row-fields">
        <label class="sched-sublabel">
          {{ t("scheduledTasks.fieldEvery") }}
          <input
            v-model.number="intervalCount"
            type="number"
            min="1"
            class="config-input config-number"
            data-testid="schedule-interval-count"
          />
        </label>
        <label class="sched-sublabel">
          &nbsp;
          <select
            v-model="intervalUnit"
            class="config-input config-select"
            data-testid="schedule-interval-unit"
          >
            <option v-for="u in UNITS" :key="u" :value="u">
              {{ t(`scheduledTasks.${UNIT_LABEL[u]}`) }}
            </option>
          </select>
        </label>
      </div>
      <div class="config-comment">{{ t("scheduledTasks.fieldFirstRunDesc") }}</div>
      <div class="sched-row-fields">
        <label class="sched-sublabel">
          {{ t("scheduledTasks.fieldFirstRun") }} - {{ t("scheduledTasks.fieldDate") }}
          <ElDatePicker
            v-model="firstRunAtDate"
            type="date"
            format="YYYY-MM-DD"
            :teleported="false"
            data-testid="schedule-first-run-date"
          />
        </label>
        <label class="sched-sublabel">
          {{ t("scheduledTasks.fieldTime") }}
          <ElTimePicker
            v-model="firstRunAtTime"
            format="HH:mm"
            :teleported="false"
            data-testid="schedule-first-run-time"
          />
        </label>
      </div>
    </template>

    <!-- daily / weekly: a wall clock, and for weekly the day it lands on -->
    <div v-else-if="mode === 'daily' || mode === 'weekly'" class="sched-row-fields">
      <label v-if="mode === 'weekly'" class="sched-sublabel">
        {{ t("scheduledTasks.fieldWeekday") }}
        <select
          v-model="weekday"
          class="config-input config-select"
          data-testid="schedule-weekday"
        >
          <option v-for="d in WEEKDAYS" :key="d" :value="d">
            {{ t(`scheduledTasks.weekday.${d}`) }}
          </option>
        </select>
      </label>
      <label class="sched-sublabel">
        {{ t("scheduledTasks.fieldTime") }}
        <ElTimePicker
          v-model="timeOfDayDate"
          format="HH:mm"
          :clearable="false"
          :teleported="false"
          data-testid="schedule-time-picker"
        />
      </label>
    </div>

    <!-- advanced: the raw string, untouched -->
    <template v-else>
      <div class="config-comment">{{ t("scheduledTasks.advancedDesc") }}</div>
      <input
        v-model="rawText"
        type="text"
        class="config-input"
        data-testid="schedule-raw-input"
      />
    </template>

    <p class="sched-preview" data-testid="schedule-preview">
      <span class="sched-preview__label">{{ t("scheduledTasks.schedulePreview") }}</span>
      <code class="sched-preview__value">{{ generated || "—" }}</code>
    </p>
  </div>
</template>

<style scoped>
/* Layout only — colours, spacing and radii all come from the design tokens in
   `styles/variables.css`; `.config-field` / `.config-label` / `.config-input`
   / `.config-select` / `.config-number` / `.config-comment` come from the
   global `styles/common/settings.css` and are deliberately not redefined. */

/* Mode selector: a flat segmented row. The global `.mode-btn` is icon-only
   (`.mode-btn-label { display: none }`), so it cannot carry these five text
   labels — hence a local, minimal segmented control. */
.sched-modes {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
  margin-bottom: var(--space-2);
}
.sched-mode {
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-tertiary);
  color: var(--text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: background 140ms cubic-bezier(0.22, 1, 0.36, 1),
    border-color 140ms cubic-bezier(0.22, 1, 0.36, 1);
}
.sched-mode:hover:not(.sched-mode--on) {
  background: var(--bg-hover);
  border-color: var(--border-light);
}
.sched-mode:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
.sched-mode--on {
  border-color: var(--accent);
  background: var(--accent-light);
  color: var(--text-primary);
  font-weight: 600;
}

/* Field rows: label above control, rows wrap on narrow modals. */
.sched-row-fields {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: var(--space-2);
}
.sched-sublabel {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  font-size: var(--text-sm);
  color: var(--text-secondary);
}

/* Preview: the exact string that will be sent, so the generated syntax is
   never a black box (and stays copy-pasteable into advanced mode). */
.sched-preview {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  margin: 0;
  font-size: var(--text-xs);
  color: var(--text-secondary);
}
.sched-preview__value {
  font-family: var(--font-mono, monospace);
  color: var(--text-primary);
  word-break: break-all;
}

/* element-plus theming — map its ``--el-*`` variables onto our design tokens
   so the picker reads as part of this app rather than a third-party widget.
   Element-plus honours ``html.dark`` (see main.ts + useTheme) for its own
   palette, so we only ride on top with brand + surface tweaks — no manual
   light/dark branching here. ``:deep`` is required because the picker's
   markup carries no scope id, and the pickers are ``teleported="false"`` so
   these overrides reach the popover too. */
.sched-sublabel :deep(.el-input__wrapper),
.sched-sublabel :deep(.el-date-editor.el-input),
.sched-sublabel :deep(.el-date-editor.el-input__wrapper) {
  --el-input-bg-color: var(--bg-input);
  --el-input-border-color: var(--border);
  --el-input-hover-border-color: var(--border-light);
  --el-input-focus-border-color: var(--accent);
  --el-input-text-color: var(--text-primary);
  --el-fill-color-blank: var(--bg-input);
  --el-border-color: var(--border);
  --el-text-color-regular: var(--text-primary);
  min-width: 190px;
}
/* The popover panel: use our surface colour so the picker blends into the
   modal rather than sitting on a stock element-plus grey square. */
.sched-sublabel :deep(.el-picker-panel) {
  --el-bg-color-overlay: var(--bg-secondary);
  --el-border-color-light: var(--border);
  --el-text-color-primary: var(--text-primary);
  --el-text-color-regular: var(--text-primary);
  --el-text-color-secondary: var(--text-secondary);
  --el-color-primary: var(--accent);
  --el-fill-color-blank: var(--bg-input);
}
/* The two boxes at the top of the panel (date input on the left, time input
   on the right, ``.el-date-picker__time-header`` sits inside .el-picker-panel).
   Default sizing gives each box 50% of the panel width, which visibly bleeds
   past the calendar — the time box only shows ``HH:mm`` and doesn't need
   half the width. Trim its input padding + shrink its inner min-width so the
   pair sits INSIDE the calendar. */
.sched-sublabel :deep(.el-date-picker__time-header) .el-input__wrapper {
  padding: 0 8px;
}
.sched-sublabel :deep(.el-date-picker__time-header) .el-input__inner {
  min-width: 0;
  text-align: center;
}
</style>
