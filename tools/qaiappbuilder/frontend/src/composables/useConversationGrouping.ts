// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useConversationGrouping — V1-parity five-bucket time grouping for the
 * Recent Chats list.
 *
 * Extracted from `AppSidebar.vue` (cohesion split). Buckets and per-bucket
 * cap mirror V1 `useChat.js:288-378`:
 *   • today / yesterday / thisWeek (7d) / thisMonth (30d) / earlier
 *   • each bucket caps at 5 items by default with an "expanded" flag the
 *     UI flips when the user clicks "{n} more" / "Collapse".
 *
 * The composable is purely state + computed: no `watch`, no lifecycle
 * hooks, no module-level mutables. Safe to call inside any setup
 * function. The caller passes the source `conversations` ref/computed
 * and gets back the grouped buckets and a per-bucket toggle.
 */
import { computed, ref, type ComputedRef, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import type { ConversationSummary } from "@/stores/conversations";

export type HistoryGroupKey =
  | "pinned"
  | "today"
  | "yesterday"
  | "thisWeek"
  | "thisMonth"
  | "earlier";

export interface HistoryGroup {
  key: HistoryGroupKey;
  label: string;
  items: ConversationSummary[];
  total: number;
  expanded: boolean;
}

// The pinned group is rendered ABOVE the time buckets and is intentionally
// not part of the time-bucket ordering / cap (it holds the conversations the
// user is actively working on and must always be fully visible on top).
const CONV_GROUP_ORDER: readonly HistoryGroupKey[] = [
  "today",
  "yesterday",
  "thisWeek",
  "thisMonth",
  "earlier",
] as const;

// V1 useChat.js:291 — show at most 5 items per bucket; remainder
// behind a "{n} more" / "Collapse" toggle. Exported so the sidebar
// template can show "{N} more" / "Collapse" labels using the same cap.
export const CONV_GROUP_CAP = 5;

/**
 * V1 convGroupLabel (useChat.js:294-306) — boundaries are calendar
 * based, not "diff from now": today is "since 00:00 today", yesterday
 * is the previous calendar day, this week is the trailing 7-day window
 * (today + 6 days), this month is the trailing 30-day window
 * (today + 29 days). Anything older falls into "earlier".
 */
function groupKeyFor(iso: string): HistoryGroupKey {
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return "earlier";
  const now = new Date();
  const todayStart = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
  ).getTime();
  const yesterdayStart = todayStart - 86400000;
  const weekStart = todayStart - 6 * 86400000;
  const monthStart = todayStart - 29 * 86400000;
  if (ts >= todayStart) return "today";
  if (ts >= yesterdayStart) return "yesterday";
  if (ts >= weekStart) return "thisWeek";
  if (ts >= monthStart) return "thisMonth";
  return "earlier";
}

export interface UseConversationGroupingReturn {
  groupedConversations: ComputedRef<HistoryGroup[]>;
  toggleGroupExpanded: (key: HistoryGroupKey, value: boolean) => void;
}

export function useConversationGrouping(
  conversations:
    | Ref<readonly ConversationSummary[]>
    | ComputedRef<readonly ConversationSummary[]>,
  options: {
    /**
     * When this flag is `true`, `groupedConversations` STOPS recomputing and
     * keeps returning the last snapshot taken while the flag was `false`
     * (bug1 fix). The sidebar passes the rename-dialog-open flag here so the
     * conversation rows do not re-sort / cross time-buckets while the user is
     * mid-rename — a moving `.conv-item` (driven by the streaming live-status
     * dot updating `updated_at` ordering every frame) was making the pointer
     * land on the dialog backdrop and dismiss it before confirm.
     *
     * Optional: when omitted the list stays fully live (no behaviour change
     * for any other caller).
     */
    freeze?: Ref<boolean>;
  } = {},
): UseConversationGroupingReturn {
  const { t } = useI18n();

  // Per-group expand flag (V1 useChat.js:288). Keys are HistoryGroupKey.
  const convGroupExpanded = ref<Record<string, boolean>>({});

  // Last live result, captured on every live recompute. While `freeze` is
  // true the computed returns this frozen snapshot instead of re-sorting.
  let frozenSnapshot: HistoryGroup[] | null = null;

  function computeLive(): HistoryGroup[] {
    const buckets: Record<HistoryGroupKey, ConversationSummary[]> = {
      pinned: [],
      today: [],
      yesterday: [],
      thisWeek: [],
      thisMonth: [],
      earlier: [],
    };
    for (const c of conversations.value) {
      // Pinned conversations are lifted into the dedicated top group and
      // removed from their time bucket (no duplication — a pinned chat shows
      // once, on top). Unpinning returns it to its natural time bucket.
      if (c.pinned === true) {
        buckets.pinned.push(c);
        continue;
      }
      buckets[groupKeyFor(c.updated_at)].push(c);
    }
    const groups: HistoryGroup[] = [];
    // Pinned group first (only when non-empty), fully expanded / uncapped.
    if (buckets.pinned.length > 0) {
      const pinnedItems = buckets.pinned
        .slice()
        .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at));
      groups.push({
        key: "pinned",
        label: t("time.pinned"),
        items: pinnedItems,
        total: pinnedItems.length,
        expanded: true,
      });
    }
    for (const key of CONV_GROUP_ORDER) {
      if (buckets[key].length === 0) continue;
      // Sort within bucket by updated_at desc (newer first).
      const all = buckets[key]
        .slice()
        .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at));
      const expanded = convGroupExpanded.value[key] === true;
      groups.push({
        key,
        // i18n key mirrors V1 SidebarPanel.js:112 (`time.<bucket>`).
        label: t(`time.${key}`),
        items: expanded ? all : all.slice(0, CONV_GROUP_CAP),
        total: all.length,
        expanded,
      });
    }
    return groups;
  }

  const groupedConversations = computed<HistoryGroup[]>(() => {
    // Frozen: return the snapshot taken just before freezing. We deliberately
    // do NOT read `conversations.value` here so streaming `updated_at` churn
    // does not re-trigger this computed while the rename dialog is open.
    if (options.freeze?.value === true && frozenSnapshot !== null) {
      return frozenSnapshot;
    }
    const live = computeLive();
    // Keep the latest live result so the first frozen render reuses it.
    frozenSnapshot = live;
    return live;
  });

  function toggleGroupExpanded(key: HistoryGroupKey, value: boolean): void {
    convGroupExpanded.value = { ...convGroupExpanded.value, [key]: value };
  }

  return {
    groupedConversations,
    toggleGroupExpanded,
  };
}
