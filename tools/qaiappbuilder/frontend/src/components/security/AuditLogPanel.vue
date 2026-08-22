<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * AuditLogPanel — Security audit tab (audit log ONLY).
 *
 * Renders the append-only audit log: paginated table + multi-dimension
 * filter (decision / op / source / origin / channel / path) + CSV
 * export. The 5s auto-refresh checkbox re-pulls the current page.
 *
 * The former sub-blocks (subject-scoped grants + pending permission
 * requests) moved to the standalone Authorization tab
 * (``GrantsPanel.vue``) — those surfaces are "revocable state" +
 * "to-do queue", conceptually distinct from the read-only audit log,
 * so co-locating them here caused cognitive overload. See
 * ``SecurityView.vue`` for the ``authorize`` tab.
 *
 * Uses global CSS classes from security.css (.sec-cfg-audit-*).
 */
import {
  ref,
  computed,
  onActivated,
  onBeforeUnmount,
  onDeactivated,
  onMounted,
  watch,
} from "vue";
import { useI18n } from "vue-i18n";

import { apiJson, ApiError } from "@/api";
import { useAuditFilter, classifyAuditOrigin } from "@/composables/useAuditFilter";
import { useSecurityStore } from "@/stores/security";

const { t } = useI18n();
const securityStore = useSecurityStore();

// ─── Types ───────────────────────────────────────────────────────────────────

interface SubjectDTO {
  kind: "user" | "preset" | "system";
  identifier: string;
}

interface ResourceDTO {
  kind: "path" | "skill" | "network" | "exec" | "dep";
  identifier: string;
}

interface AuditEntry {
  audit_id: string;
  occurred_at: string;
  subject: SubjectDTO;
  resource: ResourceDTO;
  decision: "allow" | "deny";
  rule_id: string | null;
  correlation_id: string | null;
  note: string;
  channel?: string | null;
  // ── Tail-appended native-actor metadata (SEC-ENHANCE-AUDITUX 3-B). ────────
  // OPTIONAL: undefined on old rows / non-native events.
  op?: string;
  process_path?: string;
  command_line?: string;
  actor_pid?: number | null;
  actor_parent_pid?: number | null;
}

interface AuditResponse {
  entries: AuditEntry[];
  // Tail-appended by the paginated backend (see interfaces/http/routes/
  // security/_dto.py AuditRecentResponse). Older stubs may omit it; the
  // fetcher below reads defensively.
  total?: number;
}





// ─── State ───────────────────────────────────────────────────────────────────

const loading = ref(false);
const entries = ref<AuditEntry[]>([]);
const fetchError = ref<string | null>(null);

// Multi-dimension audit filter (V1 SecurityConfigPanel audit parity).
//
// V1 filters on flat fields decision/op/channel + a path search with
// substring/wildcard/regex modes. The matching logic + filter refs are
// extracted into `useAuditFilter` (composable, unit-testable in isolation)
// so this file only owns IO/template concerns. See that file for the
// per-dimension semantics over V2's structured `_AuditEntryDTO`.
const {
  filterDecision,
  filterOp,
  filterSource,
  filterOrigin,
  filterChannel,
  filterText,
  pathMode,
  pathFilterInvalid,
  filteredEntries,
} = useAuditFilter(entries);

// ─── Pagination state ────────────────────────────────────────────────────────
//
// Backend paginates via ``limit`` + ``offset`` and returns ``total`` for
// the current time-window. The 4 page-size choices mirror common table
// conventions (25 / 50 / 100 / 200) and are all well under the backend
// cap (500). Client-side filter refs are already destructured above; we
// derive ``hasClientFilter`` here so the paged-count subtitle surfaces
// "M of N (filtered within page)" honestly.
const PAGE_SIZE_CHOICES = [25, 50, 100, 200] as const;
type AuditPageSize = (typeof PAGE_SIZE_CHOICES)[number];
const pageSize = ref<AuditPageSize>(50);
const pageIndex = ref(0); // 0-based
const totalRows = ref(0);
const totalPages = computed(() =>
  Math.max(1, Math.ceil(totalRows.value / pageSize.value)),
);
const rangeStart = computed(() =>
  totalRows.value === 0 ? 0 : pageIndex.value * pageSize.value + 1,
);
const rangeEnd = computed(() =>
  Math.min(totalRows.value, (pageIndex.value + 1) * pageSize.value),
);
const hasClientFilter = computed(
  () =>
    !!filterDecision.value ||
    !!filterOp.value ||
    !!filterSource.value ||
    !!filterOrigin.value ||
    !!filterChannel.value ||
    !!filterText.value.trim(),
);

// Auto-refresh (V1 "Auto (5s)", default off).
const autoRefresh = ref(false);
let autoRefreshHandle: ReturnType<typeof setInterval> | null = null;




// ─── Actions ─────────────────────────────────────────────────────────────────

async function fetchAuditLogs(): Promise<void> {
  loading.value = true;
  fetchError.value = null;
  try {
    const qs = new URLSearchParams({
      limit: String(pageSize.value),
      offset: String(pageIndex.value * pageSize.value),
    });
    const res = await apiJson<AuditResponse>(
      "GET",
      `/api/security/audit/recent?${qs.toString()}`,
    );
    entries.value = res.entries ?? [];
    totalRows.value =
      typeof res.total === "number" ? res.total : entries.value.length;
    // If the server-reported total shrunk below our current offset (e.g.
    // rotation deleted rows while the user was on page 10) clamp back to
    // the last valid page and re-fetch so the table is never empty when
    // more pages exist.
    if (
      totalRows.value > 0 &&
      pageIndex.value > 0 &&
      pageIndex.value * pageSize.value >= totalRows.value
    ) {
      pageIndex.value = Math.max(0, totalPages.value - 1);
      await fetchAuditLogs();
    }
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      entries.value = [];
      totalRows.value = 0;
      fetchError.value = null; // Graceful — endpoint not yet active
    } else {
      fetchError.value = (e as Error).message || "Failed to load audit logs";
    }
  } finally {
    loading.value = false;
  }
}

function gotoPage(target: number): void {
  const clamped = Math.max(0, Math.min(totalPages.value - 1, target));
  if (clamped === pageIndex.value) return;
  pageIndex.value = clamped;
  void fetchAuditLogs();
}

function onPageSizeChange(next: AuditPageSize): void {
  if (next === pageSize.value) return;
  pageSize.value = next;
  pageIndex.value = 0; // Reset to first page when page size changes.
  void fetchAuditLogs();
}

async function exportCsv(): Promise<void> {
  // Export the full audit set (not just the current page). We iterate the
  // paginated endpoint at the max page size until we've collected every
  // row the backend reports. Client-side filters STILL apply — this is
  // "export what I see, expanded across all pages".
  const BACKEND_MAX_LIMIT = 500;
  const all: AuditEntry[] = [];
  let offset = 0;
  let total = totalRows.value;
  try {
    // First fetch establishes ``total``; subsequent pages walk it.
    do {
      const qs = new URLSearchParams({
        limit: String(BACKEND_MAX_LIMIT),
        offset: String(offset),
      });
      const res = await apiJson<AuditResponse>(
        "GET",
        `/api/security/audit/recent?${qs.toString()}`,
      );
      const batch = res.entries ?? [];
      all.push(...batch);
      if (typeof res.total === "number") total = res.total;
      offset += batch.length;
      // Defensive: server returned no rows but claimed more remaining —
      // avoid an infinite loop.
      if (batch.length === 0) break;
    } while (offset < total);
  } catch (e) {
    fetchError.value = (e as Error).message || "Failed to export audit logs";
    return;
  }
  // Reuse the existing filter pipeline over the exported set so the CSV
  // reflects the user's active filters — not just the current page.
  const originMatches = (id: string): boolean =>
    !filterOrigin.value || classifyAuditOrigin(id) === filterOrigin.value;
  const rows = all.filter(
    (e) =>
      (!filterDecision.value || e.decision === filterDecision.value) &&
      (!filterOp.value ||
        (e.op && e.op.trim() !== "" ? e.op : e.resource.kind) ===
          filterOp.value) &&
      (!filterSource.value || e.subject.kind === filterSource.value) &&
      originMatches(e.subject.identifier),
  );
  const header =
    "Time,Operation,Resource,Decision,Source,Origin,Process,PID,Channel,Reason\n";
  const csv = rows
    .map((e) =>
      [
        e.occurred_at,
        e.op && e.op.trim() !== "" ? e.op : e.resource.kind,
        `"${e.resource.identifier.replace(/"/g, '""')}"`,
        e.decision,
        e.subject.identifier,
        classifyAuditOrigin(e.subject.identifier),
        `"${(e.process_path ?? "").replace(/"/g, '""')}"`,
        e.actor_pid ?? "",
        e.channel ?? "",
        `"${(e.note ?? "").replace(/"/g, '""')}"`,
      ].join(","),
    )
    .join("\n");
  const blob = new Blob([header + csv], { type: "text/csv" });
  downloadBlob(blob, "audit-log.csv");
}

function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

// ── Native-actor metadata surfacing (SEC-ENHANCE-AUDITUX 3-A / 3-B) ───────────

/**
 * Human badge label for an entry's origin. Shares `classifyAuditOrigin`
 * with the filter composable so the badge and the Origin dropdown never
 * drift. `other` → no badge (returns null).
 */
function originBadge(entry: AuditEntry): { label: string; cls: string } | null {
  const origin = classifyAuditOrigin(entry.subject.identifier);
  if (origin === "in-process")
    return { label: t("security.auditPanel.originInProcessShort"), cls: "sec-audit-origin--in-process" };
  if (origin === "native")
    return { label: t("security.auditPanel.originNativeShort"), cls: "sec-audit-origin--native" };
  return null;
}

/** Basename of the native process image path (e.g. `git.exe`). */
function processBasename(p: string | undefined): string {
  if (!p) return "";
  const parts = p.split(/[\\/]/);
  return parts[parts.length - 1] || p;
}

/** Effective operation label: prefer the concrete `op`, fall back to resource.kind. */
function opLabel(entry: AuditEntry): string {
  return entry.op && entry.op.trim() !== "" ? entry.op : entry.resource.kind;
}

/** True when the entry carries any native-actor metadata worth surfacing. */
function hasNativeMeta(entry: AuditEntry): boolean {
  return (
    (entry.process_path != null && entry.process_path !== "") ||
    (entry.command_line != null && entry.command_line !== "") ||
    entry.actor_pid != null
  );
}

// ─── Lifecycle ───────────────────────────────────────────────────────────────

onMounted(() => {
  void fetchAuditLogs();
});

// Auto-refresh (5s interval while enabled): re-pull the current
// page. The grants + pending sub-blocks lived here in V1 and had a
// paired refresh tick; they moved to the Authorization tab and now
// own their own timer.
function startAutoRefresh(): void {
  if (autoRefreshHandle !== null) return;
  autoRefreshHandle = setInterval(() => {
    void fetchAuditLogs();
  }, 5000);
}
function stopAutoRefresh(): void {
  if (autoRefreshHandle !== null) {
    clearInterval(autoRefreshHandle);
    autoRefreshHandle = null;
  }
}
watch(autoRefresh, (on) => {
  if (on) startAutoRefresh();
  else stopAutoRefresh();
});
// Lifecycle is KeepAlive-aware: SecurityView is cached by AppMain.vue's
// <KeepAlive>, so we must pause the 5s auto-refresh timer on `onDeactivated`
// (hide) and resume on `onActivated` (show, only if the user still has
// auto-refresh toggled on). Cleaning up only in `onBeforeUnmount` would let
// the timer keep hammering /api/security/audit-logs every 5s forever after
// the user navigates away from /security.
onDeactivated(stopAutoRefresh);
onActivated(() => {
  // 2026-07 fix: switching back to the audit tab MUST refetch immediately.
  // Before this, ``onActivated`` only re-armed the auto-refresh timer (when
  // it was toggled on), so the table kept showing the snapshot from the
  // last activation — even after minutes of new decisions had landed in
  // ``security_audit_entry``. Users assumed the audit stopped recording;
  // the row was there in SQLite the whole time.
  void fetchAuditLogs();
  if (autoRefresh.value) startAutoRefresh();
});
onBeforeUnmount(stopAutoRefresh);
</script>

<template>
  <div class="sec-cfg-audit-block">
    <!-- Header -->
    <div class="sec-cfg-block-header">
      <!-- Paginated title: shows total row count in the current time-window
           so users know how many records exist beyond the visible page.
           i18n key ``security.auditLogTitleN`` accepts ``{n}`` for the
           server-reported ``total``. -->
      <span class="sec-cfg-block-title">
        {{ t("security.auditLogTitleN", { n: totalRows }) }}
      </span>
      <div class="sec-cfg-audit-controls">
        <select
          v-model="filterDecision"
          class="config-input sec-audit-filter-select"
          data-testid="audit-filter-decision"
        >
          <option value="">
            {{ t("security.filterAllDecisions") }}
          </option>
          <option value="allow">
            allow
          </option>
          <option value="deny">
            deny
          </option>
        </select>
        <select
          v-model="filterOp"
          class="config-input sec-audit-filter-select"
          data-testid="audit-filter-op"
        >
          <option value="">
            {{ t("security.filterAllOps") }}
          </option>
          <option value="path">
            path
          </option>
          <option value="skill">
            skill
          </option>
          <option value="network">
            network
          </option>
          <option value="exec">
            exec
          </option>
          <option value="dep">
            dep
          </option>
        </select>
        <select
          v-model="filterSource"
          class="config-input sec-audit-filter-select"
          data-testid="audit-filter-source"
        >
          <option value="">
            {{ t("security.auditPanel.allSources") }}
          </option>
          <option value="user">
            user
          </option>
          <option value="preset">
            preset
          </option>
          <option value="system">
            system
          </option>
        </select>
        <select
          v-model="filterOrigin"
          class="config-input sec-audit-filter-select"
          data-testid="audit-filter-origin"
          :title="t('security.auditPanel.originFilterTitle')"
        >
          <option value="">
            {{ t("security.auditPanel.allOrigins") }}
          </option>
          <option value="in-process">
            {{ t("security.auditPanel.originInProcess") }}
          </option>
          <option value="native">
            {{ t("security.auditPanel.originNative") }}
          </option>
        </select>
        <select
          class="config-input sec-audit-filter-select"
          data-testid="audit-filter-channel"
        >
          <option value="">
            {{ t("security.auditPanel.allChannels") }}
          </option>
          <option value="web">
            web
          </option>
          <option value="wechat">
            wechat
          </option>
          <option value="feishu">
            feishu
          </option>
          <option value="cli">
            cli
          </option>
          <option value="background">
            background
          </option>
        </select>
        <select
          v-model="pathMode"
          class="config-input sec-audit-filter-select"
          data-testid="audit-filter-pathmode"
          :title="t('security.auditPanel.pathMatchModeTitle')"
        >
          <option value="substring">
            {{ t("security.filterSubstring") }}
          </option>
          <option value="wildcard">
            {{ t("security.filterWildcard") }}
          </option>
          <option value="regex">
            {{ t("security.filterRegex") }}
          </option>
        </select>
        <input
          v-model="filterText"
          type="text"
          class="sec-cfg-audit-pathfilter"
          :class="{ 'sec-cfg-audit-pathfilter--invalid': pathFilterInvalid }"
          :placeholder="pathMode === 'regex' ? t('security.auditPanel.regexPlaceholder') : pathMode === 'wildcard' ? t('security.auditPanel.wildcardPlaceholder') : t('security.auditPanel.pathFilterPlaceholder')"
          :title="pathFilterInvalid ? t('security.auditPanel.invalidRegex') : ''"
          data-testid="audit-filter-text"
        />
        <label
          class="sec-audit-autorefresh"
          :title="t('security.autoRefresh')"
        >
          <input
            v-model="autoRefresh"
            type="checkbox"
            data-testid="audit-autorefresh"
          />
          {{ t("security.autoRefresh") }}
        </label>
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          :disabled="loading"
          data-testid="audit-refresh"
          @click="fetchAuditLogs"
        >
          {{ t("common.refresh") }}
        </button>
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          :disabled="filteredEntries.length === 0"
          @click="exportCsv"
        >
          {{ t("security.exportAuditBtn") }}
        </button>
      </div>
    </div>

    <!-- Pagination bar (page size + prev/next + range indicator).
         Rendered on its own row below the filter/action strip so it
         stays legible on narrow layouts. Hidden when there is nothing
         to paginate. -->
    <div
      v-if="totalRows > 0"
      class="sec-cfg-audit-pagination"
      data-testid="audit-pagination"
    >
      <label class="sec-cfg-audit-pagesize">
        {{ t("security.auditPagination.pageSizeLabel") }}
        <select
          class="config-input sec-audit-filter-select"
          :value="pageSize"
          data-testid="audit-pagesize"
          @change="onPageSizeChange(Number(($event.target as HTMLSelectElement).value) as AuditPageSize)"
        >
          <option
            v-for="opt in PAGE_SIZE_CHOICES"
            :key="opt"
            :value="opt"
          >
            {{ opt }}
          </option>
        </select>
      </label>
      <span
        class="sec-cfg-audit-range"
        data-testid="audit-pagination-range"
      >
        {{ t("security.auditPagination.range", {
          start: rangeStart,
          end: rangeEnd,
          total: totalRows,
        }) }}
        <span
          v-if="hasClientFilter"
          class="sec-cfg-audit-filter-note"
          :title="t('security.auditPagination.filterWithinPageTitle')"
        >
          {{ t("security.auditPagination.filterWithinPage") }}
        </span>
      </span>
      <div class="sec-cfg-audit-pagenav">
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          :disabled="pageIndex === 0 || loading"
          data-testid="audit-page-first"
          @click="gotoPage(0)"
        >
          {{ t("security.auditPagination.first") }}
        </button>
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          :disabled="pageIndex === 0 || loading"
          data-testid="audit-page-prev"
          @click="gotoPage(pageIndex - 1)"
        >
          {{ t("security.auditPagination.prev") }}
        </button>
        <span
          class="sec-cfg-audit-pageindicator"
          data-testid="audit-page-indicator"
        >
          {{ t("security.auditPagination.pageOf", {
            page: pageIndex + 1,
            pages: totalPages,
          }) }}
        </span>
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          :disabled="pageIndex >= totalPages - 1 || loading"
          data-testid="audit-page-next"
          @click="gotoPage(pageIndex + 1)"
        >
          {{ t("security.auditPagination.next") }}
        </button>
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          :disabled="pageIndex >= totalPages - 1 || loading"
          data-testid="audit-page-last"
          @click="gotoPage(totalPages - 1)"
        >
          {{ t("security.auditPagination.last") }}
        </button>
      </div>
    </div>

    <!-- Loading -->
    <p
      v-if="loading && entries.length === 0"
      class="config-comment"
    >
      {{ t("common.loading") }}
    </p>

    <!-- Error -->
    <p
      v-else-if="fetchError"
      class="config-comment"
      style="color: var(--error);"
    >
      {{ fetchError }}
    </p>

    <!-- Table -->
    <div
      v-else-if="filteredEntries.length > 0"
      class="sec-cfg-audit-tablewrap"
    >
      <table class="sec-cfg-audit-table">
        <thead>
          <tr>
            <th>{{ t("security.auditColTime") }}</th>
            <th>{{ t("security.auditColOp") }}</th>
            <th>{{ t("security.auditColPath") }}</th>
            <th>{{ t("security.auditColDecision") }}</th>
            <th>{{ t("security.auditColSource") }}</th>
            <th>{{ t("security.auditColChannel") }}</th>
            <!-- V1 SecurityConfigPanel.js:1492-1494 parity: Reason column
                 surfaces the per-row policy-hit / deny reason. The V2 audit
                 DTO carries this on `entry.note` (see _AuditEntryDTO).
                 We render an em-dash placeholder when empty so the column
                 stays vertically aligned. -->
            <th>{{ t("security.auditColReason") }}</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="entry in filteredEntries"
            :key="entry.audit_id"
          >
            <td>{{ formatTime(entry.occurred_at) }}</td>
            <td>{{ opLabel(entry) }}</td>
            <td class="sec-cfg-audit-path">
              {{ entry.resource.identifier }}
            </td>
            <td>
              <span
                class="sec-cfg-decision"
                :class="`sec-cfg-decision--${entry.decision}`"
              >
                {{ entry.decision }}
              </span>
            </td>
            <td class="sec-cfg-audit-source">
              <div class="sec-audit-source-main">
                <span
                  v-if="originBadge(entry)"
                  class="sec-audit-origin-badge"
                  :class="originBadge(entry)!.cls"
                  data-testid="audit-origin-badge"
                >{{ originBadge(entry)!.label }}</span>
                <span class="sec-audit-source-id">{{ entry.subject.identifier }}</span>
              </div>
              <!-- Native-actor metadata (3-B): process basename + pid subtitle,
                   full command line in the title tooltip. Compact, single-line;
                   only rendered for native events that actually carry the fields. -->
              <div
                v-if="hasNativeMeta(entry)"
                class="sec-audit-source-meta"
                :title="entry.command_line ?? ''"
                data-testid="audit-source-nativemeta"
              >
                <span v-if="entry.process_path">{{ processBasename(entry.process_path) }}</span>
                <span
                  v-if="entry.actor_pid != null"
                  class="sec-audit-source-pid"
                >{{ t("security.auditPanel.pidLabel", { pid: entry.actor_pid }) }}</span>
              </div>
            </td>
            <td>{{ entry.channel ?? "—" }}</td>
            <td class="sec-cfg-audit-reason">
              {{ entry.note ? entry.note : "—" }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Empty state -->
    <p
      v-else
      class="config-comment"
      style="font-style: italic; text-align: center; padding: var(--space-6);"
    >
      {{ t("security.noAuditEntries") }}
    </p>
  </div>
</template>

<style scoped>
.sec-audit-subblock {
  margin-top: var(--space-5);
}
.sec-audit-subject-kind {
  width: 110px;
}

/* Help text under a sub-panel header — muted, small, sits above the table.
 * Explains what the panel does so operators aren't left guessing. */
.sec-audit-help {
  margin: var(--space-2) 0 var(--space-3);
  font-style: italic;
}

/* Pending-row action cluster: scope select + approve / reject / cancel. */
.sec-audit-pending-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-shrink: 0;
}
.sec-audit-pending-scope {
  width: 120px;
}

/* Reason column (V1 SecurityConfigPanel.js:1492-1494 parity). Surfaces
 * `entry.note` (policy-hit / deny reason). Constrain width and wrap so a
 * verbose reason does not blow up the table layout. Uses the same muted
 * tone as other secondary columns. */
.sec-cfg-audit-reason {
  max-width: 320px;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

/* Multi-dimension audit filters (V1 parity). */
.sec-audit-filter-select {
  width: auto;
  min-width: 120px;
}

/* Source column (SEC-ENHANCE-AUDITUX 3-A / 3-B). The main line carries an
 * origin badge (进程内 / 原生) + the subject identifier; the optional second
 * line surfaces the native process basename + pid (full command line lives in
 * the cell title tooltip). Kept compact so the row height barely grows. */
.sec-cfg-audit-source {
  max-width: 260px;
}
.sec-audit-source-main {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
}
.sec-audit-source-id {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sec-audit-origin-badge {
  flex-shrink: 0;
  font-size: var(--text-xs);
  font-weight: 600;
  padding: 1px var(--space-2);
  border-radius: var(--radius-1);
  white-space: nowrap;
}
.sec-audit-origin--in-process {
  color: var(--accent);
  background: var(--accent-light);
}
.sec-audit-origin--native {
  color: var(--warning, var(--text-secondary));
  background: var(--warning-light, var(--bg-tertiary));
}
.sec-audit-source-meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: 2px;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sec-audit-source-pid {
  flex-shrink: 0;
}
.sec-audit-autorefresh {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  font-size: var(--text-xs);
  color: var(--text-secondary);
  white-space: nowrap;
}
.sec-cfg-audit-pathfilter--invalid {
  border-color: var(--error) !important;
}

/* Pending requests rows (V1 SecurityConfigPanel.js:1567-1580 — `.sec-cfg-pending-block`
 * / `.sec-cfg-pending-row` in the legacy stylesheet). One row per request with
 * op + truncated path + timestamp, plus a Cancel button on the right. */
.sec-audit-pending-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-top: var(--space-2);
}
.sec-audit-pending-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3);
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: var(--radius-2);
}
.sec-audit-pending-info {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex: 1;
  min-width: 0;
}
.sec-audit-pending-op {
  font-size: var(--text-xs);
  font-weight: 600;
  text-transform: uppercase;
  color: var(--accent);
  padding: 2px var(--space-2);
  background: var(--accent-light);
  border-radius: var(--radius-1);
  flex-shrink: 0;
}
.sec-audit-pending-path {
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}
.sec-audit-pending-meta {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  flex-shrink: 0;
}

/* Pagination bar (page-size selector + prev/next + range indicator).
   Sits directly under the filter/action strip on its own row so the
   controls stay legible on narrow layouts. Uses the existing sec-cfg-*
   design tokens for consistency with the neighbouring blocks. */
.sec-cfg-audit-pagination {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-3);
  padding: var(--space-2) 0;
  margin-bottom: var(--space-2);
  font-size: var(--font-size-sm);
  color: var(--text-secondary);
}
.sec-cfg-audit-pagesize {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
}
.sec-cfg-audit-range {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
}
.sec-cfg-audit-filter-note {
  padding: 0 var(--space-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  font-size: var(--font-size-xs);
  color: var(--text-secondary);
  cursor: help;
}
.sec-cfg-audit-pagenav {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
}
.sec-cfg-audit-pageindicator {
  min-width: 5em;
  text-align: center;
  font-variant-numeric: tabular-nums;
}
</style>
