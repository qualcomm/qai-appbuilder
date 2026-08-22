<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * GrantsPanel — Authorization tab: standing grants + pending approvals +
 * dependency-install queue.
 *
 * Groups the three "authorization state" surfaces that used to be
 * scattered across the Audit tab + Overview tab:
 *
 *   1. Sandbox / path grants (subject-scoped, revocable) — was the
 *      "🔑 临时授权" sub-block of AuditLogPanel.
 *   2. Pending permission requests — was the "in-flight requests"
 *      sub-block of AuditLogPanel.
 *   3. Dependency-install broker queue — was the dep-install card in
 *      SecurityOverviewPanel.
 *
 * The Audit tab now focuses on the append-only audit LOG (observation
 * side); this panel focuses on active authorization STATE (revocable
 * grants + pending requests + dep queue). Rationale: three different
 * user intents (inspect / approve / revoke) were conflated on one tab.
 *
 * The three blocks use the same backend endpoints and store actions
 * they used before — no wire changes.
 */
import { ref, computed, onActivated, onBeforeUnmount, onDeactivated, onMounted } from "vue";
import { useI18n } from "vue-i18n";
import { useDepBroker } from "@/composables/useDepBroker";

import { apiJson, ApiError } from "@/api";
import { useConfirm } from "@/composables/useConfirm";
import { useSecurityStore } from "@/stores/security";

const { t } = useI18n();
const securityStore = useSecurityStore();
const { confirm } = useConfirm();
const depBroker = useDepBroker();

// ─── Types (shared with AuditLogPanel; kept local to keep GrantsPanel
//     self-contained so it can render standalone) ─────────────────────────
interface SubjectDTO {
  kind: "user" | "preset" | "system";
  identifier: string;
}
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

// Well-known system subjects auto-listed on default (empty-identifier) load.
const DEFAULT_GRANT_SUBJECTS: ReadonlyArray<SubjectDTO> = [
  { kind: "system", identifier: "ai_coding" },
  { kind: "system", identifier: "ai_coding.tool" },
];

// ─── Grants state ────────────────────────────────────────────────────────
const grants = ref<PathGrant[]>([]);
const grantsLoading = ref(false);
const grantsError = ref<string | null>(null);
const grantSubjectKind = ref<"system" | "user" | "preset">("system");
const grantSubjectIdentifier = ref("");

// ─── Pending requests state ──────────────────────────────────────────────
const pendingError = ref<string | null>(null);
const pendingRequests = computed(() => securityStore.pendingRequests);
const pendingCount = computed(() => pendingRequests.value.length);

// ─── Auto-refresh (5s) ────────────────────────────────────────────────────
const autoRefresh = ref(false);
let autoRefreshHandle: ReturnType<typeof setInterval> | null = null;

// ─── Fetch: grants ────────────────────────────────────────────────────────
async function fetchGrants(): Promise<void> {
  grantsLoading.value = true;
  grantsError.value = null;
  try {
    const explicit = grantSubjectIdentifier.value.trim();
    const subjects: ReadonlyArray<SubjectDTO> = explicit
      ? [{ kind: grantSubjectKind.value, identifier: explicit }]
      : DEFAULT_GRANT_SUBJECTS;
    const seen = new Map<string, PathGrant>();
    for (const subj of subjects) {
      const qs = new URLSearchParams({
        subject_kind: subj.kind,
        subject_identifier: subj.identifier,
      });
      try {
        const res = await apiJson<GrantsListResponse>(
          "GET",
          `/api/security/path-grants?${qs.toString()}`,
        );
        for (const g of res.grants ?? []) {
          if (!seen.has(g.grant_id)) seen.set(g.grant_id, g);
        }
      } catch (e) {
        // If the caller passed an explicit subject, surface the error;
        // otherwise, one well-known subject failing shouldn't hide the
        // grants of the other.
        if (explicit) throw e;
      }
    }
    grants.value = [...seen.values()];
  } catch (e) {
    if (e instanceof ApiError && (e.status === 400 || e.status === 404)) {
      grants.value = [];
      grantsError.value = null;
    } else {
      grantsError.value = (e as Error).message || "Failed to load grants";
    }
  } finally {
    grantsLoading.value = false;
  }
}

async function revokeGrant(grant: PathGrant): Promise<void> {
  const ok = await confirm({
    title: t("security.confirmRevokeTitle"),
    message: t("security.confirmRevokeMessage", { path: grant.path }),
    confirmStyle: "danger",
    confirmText: t("common.revoke"),
    cancelText: t("common.cancel"),
  });
  if (!ok) return;
  try {
    await apiJson("DELETE", `/api/security/path-grants/${grant.grant_id}`);
    await fetchGrants();
  } catch (e) {
    grantsError.value = (e as Error).message || "Failed to revoke grant";
  }
}

function formatMask(m: AceMaskDTO): string {
  const parts: string[] = [];
  if (m.read) parts.push("R");
  if (m.write) parts.push("W");
  if (m.execute) parts.push("X");
  if (m.delete) parts.push("D");
  return parts.join("+") || "—";
}

// ─── Pending actions ─────────────────────────────────────────────────────
type PendingScope = "once" | "session" | "process" | "permanent";
const pendingScope = ref<Record<string, PendingScope>>({});

async function fetchPending(): Promise<void> {
  try {
    await securityStore.fetchPending();
  } catch (e) {
    pendingError.value = (e as Error).message || "Failed to load pending";
  }
}

async function approvePending(requestId: string): Promise<void> {
  const scope = pendingScope.value[requestId] ?? "once";
  try {
    await securityStore.approvePermission(requestId, scope);
    await fetchGrants();
  } catch (e) {
    pendingError.value = (e as Error).message || "Failed to approve request";
  }
}

async function rejectPending(requestId: string): Promise<void> {
  try {
    await securityStore.rejectPermission(requestId);
  } catch (e) {
    pendingError.value = (e as Error).message || "Failed to reject request";
  }
}

async function cancelPending(requestId: string): Promise<void> {
  try {
    await securityStore.cancelPermission(requestId);
  } catch (e) {
    pendingError.value = (e as Error).message || "Failed to cancel request";
  }
}

// Middle-truncate long paths so a row stays single-line (V1 parity).
function truncMiddle(s: string, head: number, tail: number): string {
  if (s.length <= head + tail + 1) return s;
  return `${s.slice(0, head)}…${s.slice(-tail)}`;
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

// ─── Auto-refresh wiring ──────────────────────────────────────────────────
function startAutoRefresh(): void {
  if (autoRefreshHandle !== null) return;
  autoRefreshHandle = setInterval(() => {
    if (grantSubjectIdentifier.value.trim() || true) {
      void fetchGrants();
    }
    void fetchPending();
    void depBroker.fetchPending();
  }, 5000);
}

function stopAutoRefresh(): void {
  if (autoRefreshHandle === null) return;
  clearInterval(autoRefreshHandle);
  autoRefreshHandle = null;
}

function onAutoRefreshChange(next: boolean): void {
  autoRefresh.value = next;
  if (next) startAutoRefresh();
  else stopAutoRefresh();
}

onMounted(() => {
  void fetchGrants();
  void fetchPending();
  void depBroker.fetchSettings();
  void depBroker.fetchPending();
});

onActivated(() => {
  if (autoRefresh.value) startAutoRefresh();
});
onDeactivated(stopAutoRefresh);
onBeforeUnmount(stopAutoRefresh);
</script>

<template>
  <div class="sec-cfg-audit-block">
    <!-- Header: title + intent -->
    <div class="sec-cfg-block-header">
      <span class="sec-cfg-block-title">{{ t("security.grantsPanel.title") }}</span>
      <label
        class="sec-audit-autorefresh"
        :title="t('security.autoRefresh')"
      >
        <input
          type="checkbox"
          :checked="autoRefresh"
          data-testid="grants-autorefresh"
          @change="onAutoRefreshChange(($event.target as HTMLInputElement).checked)"
        />
        {{ t("security.autoRefresh") }}
      </label>
    </div>
    <p class="config-comment">
      {{ t("security.grantsPanel.intro") }}
    </p>

    <!-- ═══ Standing grants (subject-scoped) ═══ -->
    <div class="sec-cfg-audit-block sec-audit-subblock">
      <div class="sec-cfg-block-header">
        <span class="sec-cfg-block-title">{{ t("security.temporaryGrantsTitle") }}</span>
        <div class="sec-cfg-audit-controls">
          <select
            v-model="grantSubjectKind"
            class="config-input sec-audit-filter-select"
            data-testid="grants-subject-kind"
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
            :placeholder="t('security.auditPanel.grantSubjectPlaceholder')"
            data-testid="grants-subject-identifier"
          />
          <button
            type="button"
            class="btn btn-ghost btn-sm"
            :disabled="grantsLoading"
            data-testid="grants-load"
            @click="fetchGrants"
          >
            {{ t("security.auditPanel.loadBtn") }}
          </button>
        </div>
      </div>
      <!-- Explain scope semantics — the empty-identifier default queries
           the AI coding tool's well-known subjects; typing a session id
           narrows to that conversation. Matches the wording used on the
           tooltip inside SecurityDialog so users see the same story
           across surfaces. -->
      <p class="config-comment sec-audit-help">
        {{ t("security.auditPanel.temporaryGrantsHelp") }}
      </p>
      <p
        v-if="grantsLoading"
        class="config-comment"
      >
        {{ t("common.loading") }}
      </p>
      <p
        v-else-if="grantsError"
        class="config-comment"
        style="color: var(--error);"
      >
        {{ grantsError }}
      </p>
      <table
        v-else-if="grants.length > 0"
        class="sec-cfg-audit-table"
      >
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
            v-for="g in grants"
            :key="g.grant_id"
          >
            <td
              class="sec-cfg-audit-path"
              :title="g.path"
            >
              {{ truncMiddle(g.path, 30, 30) }}
            </td>
            <td>{{ formatMask(g.mask) }}</td>
            <td>{{ g.source }}</td>
            <td>{{ g.expires_at ? formatTime(g.expires_at) : "—" }}</td>
            <td>
              <button
                type="button"
                class="btn btn-ghost btn-sm"
                :title="t('common.revoke')"
                @click="revokeGrant(g)"
              >
                {{ t("common.revoke") }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <p
        v-else
        class="config-comment"
        style="font-style: italic; text-align: center; padding: var(--space-4);"
      >
        {{ t("security.auditPanel.noGrantsForSubject") }}
      </p>
    </div>

    <!-- ═══ Pending permission requests ═══ -->
    <div
      v-if="pendingCount > 0"
      class="sec-cfg-audit-block sec-audit-subblock"
    >
      <div class="sec-cfg-block-header">
        <span class="sec-cfg-block-title">
          {{ t("security.pendingRequestsTitle", { n: pendingCount }) }}
        </span>
      </div>
      <p
        v-if="pendingError"
        class="config-comment"
        style="color: var(--error);"
      >
        {{ pendingError }}
      </p>
      <p class="config-comment">
        {{ t("security.auditPanel.pendingRequestsHelp") }}
      </p>
      <div
        class="sec-audit-pending-list"
        data-testid="grants-pending-list"
      >
        <div
          v-for="req in pendingRequests"
          :key="req.request_id"
          class="sec-audit-pending-row"
          data-testid="grants-pending-row"
        >
          <div class="sec-audit-pending-info">
            <span class="sec-audit-pending-op">{{ req.resource.kind }}</span>
            <span
              class="sec-audit-pending-path"
              :title="req.resource.identifier"
            >
              {{ truncMiddle(req.resource.identifier, 30, 30) }}
            </span>
            <span class="sec-audit-pending-meta">{{ formatTime(req.created_at) }}</span>
          </div>
          <div class="sec-audit-pending-actions">
            <select
              v-model="pendingScope[req.request_id]"
              class="config-input sec-audit-pending-scope"
              :title="t('security.auditPanel.approveScopeHint')"
              :aria-label="t('security.auditPanel.approveScopeHint')"
              data-testid="grants-pending-scope"
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
              data-testid="grants-pending-approve"
              @click="approvePending(req.request_id)"
            >
              {{ t("security.auditPanel.approveBtn") }}
            </button>
            <button
              type="button"
              class="btn btn-danger btn-sm"
              data-testid="grants-pending-reject"
              @click="rejectPending(req.request_id)"
            >
              {{ t("security.auditPanel.rejectBtn") }}
            </button>
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              data-testid="grants-pending-cancel"
              @click="cancelPending(req.request_id)"
            >
              {{ t("common.cancel") }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══ Dependency-install approval queue ═══

         Mirrors the same queue rendered on Overview (kept there because
         it's paired with the master toggle). Duplicated here so the
         "everything waiting for me" surface stays in ONE place.
    -->
    <div
      v-if="depBroker.pending.value.length > 0"
      class="sec-cfg-audit-block sec-audit-subblock"
    >
      <div class="sec-cfg-block-header">
        <span class="sec-cfg-block-title">
          🛡️ {{ t("depBroker.title") }} ({{ depBroker.pending.value.length }})
        </span>
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          :disabled="depBroker.loading.value"
          @click="depBroker.fetchPending()"
        >
          ↺ {{ t("common.refresh") }}
        </button>
      </div>
      <div class="sec-cfg-list-desc">
        {{ t("depBroker.description") }}
      </div>
      <div
        v-for="req in depBroker.pending.value"
        :key="req.id"
        class="sec-cfg-pending-row"
      >
        <div class="sec-cfg-pending-info">
          <code class="mono">{{ req.command_args.join(" ") }}</code>
          <span class="sec-cfg-grant-meta">{{ req.requester }} · {{ req.status }}</span>
        </div>
        <div class="sec-cfg-actions-row">
          <button
            type="button"
            class="btn btn-sm btn-primary"
            @click="depBroker.approve(req.id)"
          >
            {{ t("depBroker.approve") }}
          </button>
          <button
            type="button"
            class="btn btn-sm btn-danger"
            @click="depBroker.reject(req.id)"
          >
            {{ t("depBroker.reject") }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sec-audit-subblock {
  margin-top: var(--space-5);
}

/* Inline help text — matches the audit panel's help paragraph so the
   two surfaces read the same way. */
.sec-audit-help {
  margin: var(--space-2) 0 var(--space-3);
  font-size: var(--font-size-sm);
  color: var(--text-secondary);
  line-height: 1.55;
}

/* Pending requests: one row per request. flex row keeps the scope
   selector + action buttons on ONE line so the visual weight matches
   its intent (a to-do row, not a table). */
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
  background: var(--surface-secondary);
  border-radius: var(--radius-md);
}
.sec-audit-pending-info {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  min-width: 0;
  flex: 1;
}
.sec-audit-pending-op {
  font-size: var(--font-size-xs);
  font-weight: 600;
  text-transform: uppercase;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  background: var(--surface-tertiary);
  color: var(--text-secondary);
  flex-shrink: 0;
}
.sec-audit-pending-path {
  font-family: var(--font-mono);
  font-size: var(--font-size-sm);
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}
.sec-audit-pending-meta {
  font-size: var(--font-size-xs);
  color: var(--text-secondary);
  flex-shrink: 0;
}
.sec-audit-pending-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-shrink: 0;
}
.sec-audit-pending-scope {
  width: 120px;
}
</style>
