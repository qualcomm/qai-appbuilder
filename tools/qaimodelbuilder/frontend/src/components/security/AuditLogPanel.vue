<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * AuditLogPanel — Security audit tab (V1 parity, three sub-blocks).
 *
 * Mirrors V1 `SecurityConfigPanel.js` audit tab (lines ~1425-1580):
 *   1. Audit log table — GET /api/security/audit/recent + multi-dim filter
 *      + 5s auto-refresh + CSV export.
 *   2. Sandbox grants — subject-scoped list / revoke (V2 `subject_kind`
 *      switcher replaces V1's twin Session/Process columns; functionally
 *      equivalent and more general — see backend repo `list_for_subject`).
 *   3. Pending permission requests — in-flight `_PermissionRequestDTO`s
 *      with per-row Cancel (DELETE /api/security/permission/{id}). The
 *      block is hidden when the queue is empty (V1 SecurityConfigPanel.js:1568).
 *
 * Auto-refresh ticks all three lists in lock-step (V1 lines 580-583), but
 * skips grants when the subject identifier is empty (V2 backend rejects
 * empty `subject_identifier`).
 *
 * Note: Exec Profiles are intentionally NOT shown here — that surface lives
 * in `SkillCapabilitiesPanel` to avoid duplicating the same data on two
 * tabs (V1 only exposed it once on the Skills tab).
 *
 * Uses global CSS classes from security.css (.sec-cfg-audit-*).
 */
import {
  ref,
  reactive,
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
import { useConfirm } from "@/composables/useConfirm";
import { useSecurityStore } from "@/stores/security";

const { t } = useI18n();
const securityStore = useSecurityStore();
const { confirm } = useConfirm();

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
}

// ── Sandbox grants (subject-scoped) ───────────────────────────────────────────
//
// The backend `GET /api/security/path-grants` only supports
// `list_for_subject`, so it REQUIRES `subject_kind`
// (`user` | `preset` | `system`) and `subject_identifier`. There is no
// list-all endpoint. The canonical subjects produced by the
// dynamic-authorization bridges are:
//   • process-level (file guard) → system / ai_coding (apps/api/_permission_bridge.py)
//   • exec dangerous-command ASK → system / ai_coding.tool (apps/api/_file_guard_bridge.py:530)
//   • session-level              → user / <coding_session_id> (_channel_grant_bridge.py)
// Since there is no list-all endpoint, we query the two well-known
// `system` subjects on the default (empty-identifier) load and merge the
// results (deduped by grant_id) so exec-command grants show up without the
// operator having to know/type the `ai_coding.tool` identifier. The
// session subject's identifier is a per-conversation id we cannot know in
// advance, so the operator still types it (kind=user) + clicks Load to
// inspect a specific session's grants.

interface AceMaskDTO {
  read: boolean;
  write: boolean;
  execute: boolean;
  delete: boolean;
}

interface PathGrant {
  grant_id: string;
  subject: SubjectDTO;
  path: string;
  mask: AceMaskDTO;
  source: "user" | "auto" | "preset";
  created_at: string;
  expires_at: string | null;
}

interface GrantsListResponse {
  grants: PathGrant[];
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

// Auto-refresh (V1 "Auto (5s)", default off).
const autoRefresh = ref(false);
let autoRefreshHandle: ReturnType<typeof setInterval> | null = null;

// Grants (subject-scoped) state
const grants = ref<PathGrant[]>([]);
const grantsLoading = ref(false);
const grantsError = ref<string | null>(null);
const grantSubjectKind = ref<"system" | "user" | "preset">("system");
const grantSubjectIdentifier = ref("");

// Well-known `system` subjects auto-listed on the default (empty-identifier)
// load. Both are produced by the dynamic-authorization bridges and neither
// requires the operator to know an identifier up-front:
//   • ai_coding      → file-guard path grants (apps/api/_permission_bridge.py)
//   • ai_coding.tool → exec dangerous-command grants (_file_guard_bridge.py:530)
// Querying both and merging (deduped by grant_id) means exec-command grants
// created via the ASK popup's 本会话/本次运行/永久 choices are visible — and
// therefore revocable — by default, without the operator having to type the
// `ai_coding.tool` identifier.
const DEFAULT_GRANT_SUBJECTS: ReadonlyArray<SubjectDTO> = [
  { kind: "system", identifier: "ai_coding" },
  { kind: "system", identifier: "ai_coding.tool" },
];

// Pending permission requests (V1 SecurityConfigPanel.js:92, 370-380, 1567-1580).
//
// V1 shows a list of in-flight permission requests on the audit tab with a
// per-row Cancel button (`DELETE /api/security/permission/{id}`). The list is
// only rendered when `pendingRequests.length > 0` — there is no empty-state
// copy. Source-of-truth state lives in `useSecurityStore` so the (existing)
// store action `cancelPermission` can drive the same list elsewhere if needed.
const pendingError = ref<string | null>(null);
const pendingRequests = computed(() => securityStore.pendingRequests);
const pendingCount = computed(() => pendingRequests.value.length);

// ─── Actions ─────────────────────────────────────────────────────────────────

async function fetchAuditLogs(): Promise<void> {
  loading.value = true;
  fetchError.value = null;
  try {
    const res = await apiJson<AuditResponse>("GET", "/api/security/audit/recent");
    entries.value = res.entries ?? [];
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      entries.value = [];
      fetchError.value = null; // Graceful — endpoint not yet active
    } else {
      fetchError.value = (e as Error).message || "Failed to load audit logs";
    }
  } finally {
    loading.value = false;
  }
}

async function exportCsv(): Promise<void> {
  // Client-side CSV generation over the currently-filtered rows. (There is
  // no backend GET /api/security/audit/export route — it was never part of
  // the security router — so we build the CSV in the browser directly.)
  const rows = filteredEntries.value;
  const header = "Time,Operation,Resource,Decision,Source,Origin,Process,PID,Channel,Reason\n";
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

// ── Grants actions ────────────────────────────────────────────────────────────

function maskToString(mask: AceMaskDTO): string {
  const parts: string[] = [];
  if (mask.read) parts.push("R");
  if (mask.write) parts.push("W");
  if (mask.execute) parts.push("X");
  if (mask.delete) parts.push("D");
  return parts.length > 0 ? parts.join("") : "—";
}

/**
 * Fetch grants for a single subject. Returns [] on a graceful 404
 * (endpoint not yet active); rethrows other errors so the caller can
 * surface them.
 */
async function fetchGrantsForSubject(subject: SubjectDTO): Promise<PathGrant[]> {
  const res = await apiJson<GrantsListResponse>("GET", "/api/security/path-grants", undefined, {
    query: {
      subject_kind: subject.kind,
      subject_identifier: subject.identifier,
    },
  });
  return res.grants ?? [];
}

/**
 * Merge grant lists, deduping by grant_id (a grant_id is globally unique so
 * the same grant never legitimately appears under two subjects, but we dedup
 * defensively so a future overlap can't double-render a row).
 */
function mergeGrants(lists: PathGrant[][]): PathGrant[] {
  const byId = new Map<string, PathGrant>();
  for (const list of lists) {
    for (const g of list) byId.set(g.grant_id, g);
  }
  return Array.from(byId.values());
}

async function fetchGrants(): Promise<void> {
  grantsLoading.value = true;
  grantsError.value = null;
  try {
    const typed = grantSubjectIdentifier.value.trim();
    // When the operator has typed an identifier, honour it verbatim (single
    // subject — the existing UX for inspecting a specific session's grants).
    // Otherwise default to listing the well-known `system` subjects
    // (ai_coding + ai_coding.tool) and merge, so exec-command grants are
    // visible/revocable without any manual input.
    const subjects: SubjectDTO[] =
      typed !== ""
        ? [{ kind: grantSubjectKind.value, identifier: typed }]
        : [...DEFAULT_GRANT_SUBJECTS];
    const lists = await Promise.all(subjects.map((s) => fetchGrantsForSubject(s)));
    grants.value = mergeGrants(lists);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      grants.value = [];
      grantsError.value = null;
    } else {
      grantsError.value = (e as Error).message || "Failed to load grants";
    }
  } finally {
    grantsLoading.value = false;
  }
}

async function revokeGrant(grantId: string): Promise<void> {
  // V1 parity (SecurityConfigPanel.js:505-510): confirm before revoking a
  // grant. §3.9.2 forbids native window.confirm — use the project's
  // custom <ConfirmDialog> via useConfirm().
  const grant = grants.value.find((g) => g.grant_id === grantId);
  const ok = await confirm({
    icon: "🔑",
    title: t("security.revokeConfirmTitle"),
    message: t("security.revokeConfirmMessage", { path: grant?.path ?? "" }),
    confirmText: t("security.revokeBtn"),
    cancelText: t("common.cancel"),
    confirmStyle: "danger",
  });
  if (!ok) return;
  grantsError.value = null;
  try {
    await apiJson("DELETE", `/api/security/path-grants/${grantId}`);
    grants.value = grants.value.filter((g) => g.grant_id !== grantId);
  } catch (e) {
    grantsError.value = (e as Error).message || "Failed to revoke grant";
  }
}

// ── Pending permission requests actions ──────────────────────────────────────

/** Truncate the middle of a long path so the row stays single-line. V1 parity:
 *  `truncMiddle(p.path, 30, 30)` in SecurityConfigPanel.js:1575. */
function truncMiddle(s: string, head: number, tail: number): string {
  if (!s) return "";
  if (s.length <= head + tail + 1) return s;
  return `${s.slice(0, head)}…${s.slice(-tail)}`;
}

async function fetchPending(): Promise<void> {
  pendingError.value = null;
  try {
    await securityStore.fetchPending();
  } catch (e) {
    // V1 silently ignored failures here (SecurityConfigPanel.js:375 "无 pending
    // 端点亦不致命"). Surface the error inline so operators are not blinded,
    // but never block the rest of the audit panel.
    pendingError.value = (e as Error).message || "Failed to load pending requests";
  }
}

async function cancelPending(requestId: string): Promise<void> {
  // The audit-panel Cancel button uses the same wire as the popup dialog's
  // Cancel action (Phase 2, plan §P5): POST /api/security/permission/cancel
  // with `{ request_id }`. No confirm dialog — the user already initiated
  // the underlying request, so cancelling is a benign rollback. §3.9 only
  // forbids native confirm/alert, not "no confirm at all".
  pendingError.value = null;
  await securityStore.cancelPermission(requestId);
  if (securityStore.error) {
    pendingError.value = securityStore.error;
  }
}

// Per-row approval scope (mirrors the ASK popup's once/session/process/
// permanent vocabulary). Defaults to "once" (single-use, no persisted grant),
// the safest choice. Kept as a small map so each pending row keeps its own
// selection independently.
const pendingApproveScope = reactive<
  Record<string, "once" | "session" | "process" | "permanent">
>({});

function scopeFor(
  requestId: string,
): "once" | "session" | "process" | "permanent" {
  return pendingApproveScope[requestId] ?? "once";
}

async function approvePending(requestId: string): Promise<void> {
  // Approve a pending request straight from the audit panel (a second entry
  // point besides the ASK popup — for requests whose popup was dismissed /
  // missed). The chosen scope decides whether a PathGrant is persisted:
  // once → single-use (no grant); session/process/permanent → grant so the
  // next same-path access skips the prompt.
  pendingError.value = null;
  await securityStore.approvePermission(requestId, scopeFor(requestId));
  if (securityStore.error) {
    pendingError.value = securityStore.error;
  }
}

async function rejectPending(requestId: string): Promise<void> {
  // Reject (deny) a pending request from the audit panel. No grant is
  // persisted; the triggering tool call receives a DENY.
  pendingError.value = null;
  await securityStore.rejectPermission(requestId);
  if (securityStore.error) {
    pendingError.value = securityStore.error;
  }
}

// ─── Lifecycle ───────────────────────────────────────────────────────────────

onMounted(() => {
  void fetchAuditLogs();
  void fetchGrants();
  void fetchPending();
});

// Auto-refresh (V1 parity: 5s interval, only while enabled).
//
// V1 SecurityConfigPanel.js:578-584 refreshes audit + grants + pending in
// lock-step on the audit tab. We replicate that here. The grants endpoint
// REQUIRES a `subject_identifier`, but `fetchGrants` always supplies one —
// either the operator-typed identifier or, when empty, the well-known
// default `system` subjects (see `DEFAULT_GRANT_SUBJECTS`) — so the tick is
// always safe to fire.
function startAutoRefresh(): void {
  if (autoRefreshHandle !== null) return;
  autoRefreshHandle = setInterval(() => {
    void fetchAuditLogs();
    void fetchPending();
    void fetchGrants();
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
  if (autoRefresh.value) startAutoRefresh();
});
onBeforeUnmount(stopAutoRefresh);
</script>

<template>
  <div class="sec-cfg-audit-block">
    <!-- Header -->
    <div class="sec-cfg-block-header">
      <!-- V1 parity (SecurityConfigPanel.js:1431): the audit-log block title is
           "📜 审计日志（最近 200 条）" (security.auditLogTitle), not the short
           "审计日志" (security.audit) used for the tab label. -->
      <span class="sec-cfg-block-title">{{ t("security.auditLogTitle") }}</span>
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

    <!-- ═══ Sandbox grants (subject-scoped) ═══ -->
    <div class="sec-cfg-audit-block sec-audit-subblock">
      <div class="sec-cfg-block-header">
        <!-- V1 parity (SecurityConfigPanel.js:1521): this subject-scoped grants
             block is V1's "🔑 临时授权" block (security.temporaryGrantsTitle).
             Use that title so the 🔑 icon + wording match V1; the underlying
             subject_kind switcher is V2's improved implementation. -->
        <span class="sec-cfg-block-title">{{ t("security.temporaryGrantsTitle") }}</span>
        <div class="sec-cfg-audit-controls">
          <select
            v-model="grantSubjectKind"
            class="config-input sec-audit-subject-kind"
          >
            <option value="system">
              system
            </option>
            <option value="user">
              user
            </option>
            <option value="preset">
              preset
            </option>
          </select>
          <input
            v-model="grantSubjectIdentifier"
            type="text"
            class="sec-cfg-audit-pathfilter"
            list="sec-grant-subject-presets"
            :placeholder="t('security.auditPanel.grantSubjectPlaceholder')"
          />
          <!-- Preset identifiers for the well-known `system` subjects so the
               operator can pick the exec-command grant subject (ai_coding.tool)
               without having to remember it. Leaving the input empty lists both
               defaults at once (see fetchGrants / DEFAULT_GRANT_SUBJECTS). -->
          <datalist id="sec-grant-subject-presets">
            <option value="ai_coding" />
            <option value="ai_coding.tool" />
          </datalist>
          <button
            type="button"
            class="btn btn-ghost btn-sm"
            :disabled="grantsLoading"
            @click="fetchGrants"
          >
            {{ t("security.auditPanel.loadBtn") }}
          </button>
        </div>
      </div>

      <!-- Help text — this panel confuses users: it does NOT create grants,
           it inspects + revokes a subject's EXISTING grants (which are created
           when the operator picks 本会话/本次运行/永久 in the ASK popup). -->
      <p class="config-comment sec-audit-help">
        {{ t("security.auditPanel.temporaryGrantsHelp") }}
      </p>

      <p
        v-if="grantsError"
        class="config-comment"
        style="color: var(--error);"
      >
        {{ grantsError }}
      </p>

      <div
        v-else-if="grants.length > 0"
        class="sec-cfg-audit-tablewrap"
      >
        <table class="sec-cfg-audit-table">
          <thead>
            <tr>
              <th>{{ t("security.auditColPath") }}</th>
              <th>{{ t("security.auditPanel.maskCol") }}</th>
              <th>{{ t("security.auditColSource") }}</th>
              <th>{{ t("security.auditPanel.expiresCol") }}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="grant in grants"
              :key="grant.grant_id"
            >
              <td class="sec-cfg-audit-path">
                {{ grant.path }}
              </td>
              <td>{{ maskToString(grant.mask) }}</td>
              <td>{{ grant.source }}</td>
              <td>{{ grant.expires_at ? formatTime(grant.expires_at) : "—" }}</td>
              <td>
                <button
                  type="button"
                  class="btn btn--reject btn-sm"
                  @click="revokeGrant(grant.grant_id)"
                >
                  {{ t("security.revokeBtn") }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <p
        v-else
        class="config-comment"
        style="font-style: italic; padding: var(--space-4) 0;"
      >
        {{ t("security.auditPanel.noGrantsForSubject") }}
      </p>
    </div>

    <!-- ═══ Pending permission requests (V1 SecurityConfigPanel.js:1567-1580) ═══

         Shown only when there is at least one in-flight request — V1 has no
         empty-state copy here, the block simply disappears when the queue is
         empty so the audit tab stays compact. -->
    <div
      v-if="pendingCount > 0 || pendingError"
      class="sec-cfg-audit-block sec-audit-subblock"
    >
      <div class="sec-cfg-block-header">
        <span class="sec-cfg-block-title">
          {{ t("security.pendingRequestsTitle", { n: pendingCount }) }}
        </span>
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          data-testid="audit-pending-refresh"
          @click="fetchPending"
        >
          {{ t("common.refresh") }}
        </button>
      </div>

      <p
        v-if="pendingError"
        class="config-comment"
        style="color: var(--error);"
      >
        {{ pendingError }}
      </p>

      <!-- Help text — approve/reject here is a second entry point besides the
           ASK popup (for requests whose popup was dismissed / missed). -->
      <p
        v-if="pendingCount > 0"
        class="config-comment sec-audit-help"
      >
        {{ t("security.auditPanel.pendingRequestsHelp") }}
      </p>

      <div
        v-if="pendingCount > 0"
        class="sec-audit-pending-list"
        data-testid="audit-pending-list"
      >
        <div
          v-for="p in pendingRequests"
          :key="p.request_id"
          class="sec-audit-pending-row"
          data-testid="audit-pending-row"
        >
          <div class="sec-audit-pending-info">
            <span class="sec-audit-pending-op">{{ p.resource.kind }}</span>
            <span class="sec-audit-pending-path">{{ truncMiddle(p.resource.identifier, 30, 30) }}</span>
            <span class="sec-audit-pending-meta">{{ formatTime(p.created_at) }}</span>
          </div>
          <div class="sec-audit-pending-actions">
            <select
              v-model="pendingApproveScope[p.request_id]"
              class="config-input sec-audit-pending-scope"
              :title="t('security.auditPanel.approveScopeHint')"
              :aria-label="t('security.auditPanel.approveScopeHint')"
              data-testid="audit-pending-scope"
            >
              <option value="once">
                {{ t("security.auditPanel.scopeOnce") }}
              </option>
              <option value="session">
                {{ t("security.auditPanel.scopeSession") }}
              </option>
              <option value="process">
                {{ t("security.auditPanel.scopeProcess") }}
              </option>
              <option value="permanent">
                {{ t("security.auditPanel.scopePermanent") }}
              </option>
            </select>
            <button
              type="button"
              class="btn btn-success btn-sm"
              data-testid="audit-pending-approve"
              @click="approvePending(p.request_id)"
            >
              {{ t("security.auditPanel.approveBtn") }}
            </button>
            <button
              type="button"
              class="btn btn-danger btn-sm"
              data-testid="audit-pending-reject"
              @click="rejectPending(p.request_id)"
            >
              {{ t("security.auditPanel.rejectBtn") }}
            </button>
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              data-testid="audit-pending-cancel"
              @click="cancelPending(p.request_id)"
            >
              {{ t("common.cancel") }}
            </button>
          </div>
        </div>
      </div>
    </div>
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
</style>
