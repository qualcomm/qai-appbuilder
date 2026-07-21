// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Security Pinia store.
 *
 * S5 PR-055: wraps /api/security/* routes via apiJson.
 * Exposes: policy, pending permission requests, grants, audit log.
 */
import { defineStore } from "pinia";
import { ref } from "vue";
import { apiJson } from "@/api";
import type { components } from "@/types/api";

type PolicyResponse = components["schemas"]["PolicyResponse"];
type PendingResponse = components["schemas"]["PendingPermissionRequestsResponse"];
type PermissionRequestDTO = components["schemas"]["_PermissionRequestDTO"];
type GrantsListResponse = components["schemas"]["GrantsListResponse"];
type PathGrantDTO = components["schemas"]["_PathGrantDTO"];
type AuditRecentResponse = components["schemas"]["AuditRecentResponse"];
type AuditEntryDTO = components["schemas"]["_AuditEntryDTO"];
type PolicyRuleDTO = components["schemas"]["_PolicyRuleDTO"];
type SecurityHealthResponse = components["schemas"]["SecurityHealthResponse"];
type ApplyTemplateResponse = components["schemas"]["ApplyTemplateResponse"];

/** One built-in policy template from `GET /api/security/templates`. */
export interface PolicyTemplate {
  id: string;
  name: string;
  description: string;
  rules_count: number;
}

interface TemplatesListResponse {
  templates: PolicyTemplate[];
  total: number;
}

/** One discovered skill capability from `GET /api/security/skill-discovery`. */
export interface DiscoveredSkill {
  skill_name: string;
  capability_name: string;
  read_paths: string[];
  write_paths: string[];
  exec_paths: string[];
  trusted_binaries: string[];
  description: string;
  /** V1 parity: whether the skill is currently active/enabled. */
  active?: boolean;
}

interface SkillDiscoveryResponse {
  skills: DiscoveredSkill[];
  total: number;
  scan_status: string;
}

/** Subset of the policy toggles a PUT may patch (all optional). */
export interface PolicyToggles {
  enabled?: boolean;
  mode?: "enforce" | "audit_only";
  dynamic_authorization?: boolean;
  no_ui_channels?: string[];
}

/**
 * Security master-switch mode (3c switch-tree §6.4).
 *
 * This is the NEW master control backed by `PUT /api/security/mode`. It is
 * ORTHOGONAL to — and MUST NOT be confused with — `PolicyResponse.mode`
 * (whose value domain stays `enforce | audit_only`, the run-mode sub-switch).
 *   - `enforcing`  → guard on, run-mode sub-switch stands as-is.
 *   - `permissive` → guard on, decision core forced to `audit_only`.
 *   - `disabled`   → master switch off (⟺ `policy.enabled === false`).
 */
export type SecurityMode = "enforcing" | "permissive" | "disabled";

/** `PUT /api/security/mode` body. */
interface SetSecurityModeRequest {
  mode: SecurityMode;
}

/** `PUT /api/security/mode` payload. */
interface SecurityModeResponse {
  mode: SecurityMode;
  enabled: boolean;
  effective_run_mode: "enforce" | "audit_only";
}

const HEALTH_UNKNOWN: SecurityHealthResponse = {
  status: "unknown",
  enabled: true,
  mode: "enforce",
  test_mode: false,
  native_enabled: false,
  native_active: false,
  native_diagnostics: {},
};

export const useSecurityStore = defineStore("security", () => {
  // ─── State ─────────────────────────────────────────────────────────────────
  const policy = ref<PolicyResponse | null>(null);
  const pendingRequests = ref<PermissionRequestDTO[]>([]);
  const grants = ref<PathGrantDTO[]>([]);
  /**
   * Grants scoped to the process-level subject (`system` / `ai_coding`).
   * Kept separate from {@link grants} so the Overview tile can show a
   * stable "Process grants" count regardless of which subject the Audit
   * panel happens to be inspecting.
   */
  const processGrants = ref<PathGrantDTO[]>([]);
  /**
   * Grants scoped to per-session user subjects (V1 `grants.session`).
   *
   * V2 backend constraint: `GET /api/security/path-grants` only supports
   * `list_for_subject` and REQUIRES `subject_kind` + `subject_identifier`.
   * Session grants are stored under `subject_kind="user"` with the
   * identifier set to the coding-session id (`apps/api/_channel_grant_bridge.py`).
   * There is no list-all endpoint, so this list stays empty until a caller
   * passes a known coding-session id via {@link fetchSessionGrants}. The
   * Overview tile therefore renders `0` when no active coding session is
   * tracked — matching V1's behaviour for an empty session table rather
   * than mis-reporting the process count under a "Session grants" label.
   */
  const sessionGrants = ref<PathGrantDTO[]>([]);
  const auditEntries = ref<AuditEntryDTO[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  /** FileGuard health (Overview header pill + down/test_mode banner). */
  const health = ref<SecurityHealthResponse>({ ...HEALTH_UNKNOWN });
  /** Built-in policy templates (Overview quick-apply list). */
  const policyTemplates = ref<PolicyTemplate[]>([]);
  const loadingTemplates = ref(false);
  /** Id of the template currently being applied (null = idle). */
  const applyingTemplate = ref<string | null>(null);
  /** Discovered skill capabilities (Overview permission-summary groups). */
  const discoveredSkills = ref<DiscoveredSkill[]>([]);

  /**
   * Security master-switch mode (3c switch-tree §6.4) — the NEW master
   * control, ORTHOGONAL to {@link PolicyResponse.mode} (the run-mode
   * sub-switch). The backend exposes NO GET for this scalar, so we hold
   * the last authoritative value returned by `PUT /api/security/mode`
   * ({@link setSecurityMode}) and, on initial policy load, DERIVE it from
   * the single truth source `policy.enabled`:
   *   - `policy.enabled === false` ⟺ `disabled` (backend keeps these in
   *     lock-step; see `security_runtime_state.set_mode`).
   *   - otherwise fall back to the strict default `enforcing` — we cannot
   *     distinguish `enforcing`+audit_only-sub from `permissive` without a
   *     GET, so we bias to the strict interpretation (fail-safe, matching
   *     the backend `_DEFAULT_MODE`). Once the operator sets a mode, the
   *     PUT response becomes the truth and overrides this default.
   */
  const securityMode = ref<SecurityMode>("enforcing");

  // ─── Actions ───────────────────────────────────────────────────────────────
  async function fetchPolicy(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      policy.value = await apiJson<PolicyResponse>("GET", "/api/security/policy");
      // Derive the master-switch mode from the single truth source
      // `policy.enabled` (no GET exists for the master switch scalar):
      //   - disabled ⟺ enabled === false (backend lock-step).
      //   - re-enabled from a `disabled` cache → strict default `enforcing`.
      //   - already enabled with a known `enforcing`/`permissive` cache →
      //     keep the operator's last PUT (that is the authoritative value;
      //     `enabled` alone cannot tell the two apart).
      if (policy.value.enabled === false) {
        securityMode.value = "disabled";
      } else if (securityMode.value === "disabled") {
        securityMode.value = "enforcing";
      }
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    } finally {
      loading.value = false;
    }
  }

  async function fetchPending(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const res = await apiJson<PendingResponse>("GET", "/api/security/permission/pending");
      pendingRequests.value = res.requests;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    } finally {
      loading.value = false;
    }
  }

  async function approvePermission(
    requestId: string,
    grant: "once" | "session" | "process" | "permanent" = "once",
  ): Promise<void> {
    error.value = null;
    try {
      await apiJson("POST", `/api/security/permission/${requestId}/approve`, {
        reason: "",
        grant,
      });
      pendingRequests.value = pendingRequests.value.filter((r) => r.request_id !== requestId);
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    }
  }

  async function rejectPermission(requestId: string): Promise<void> {
    error.value = null;
    try {
      await apiJson("POST", `/api/security/permission/${requestId}/reject`, { reason: "" });
      pendingRequests.value = pendingRequests.value.filter((r) => r.request_id !== requestId);
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    }
  }

  async function cancelPermission(requestId: string): Promise<void> {
    error.value = null;
    try {
      // Phase 2 (plan §P5): the single-request cancel now goes through the
      // unified body-variant endpoint. Older backends served DELETE on
      // `/permission/{id}` — that route is gone in Phase 2, cancel is the
      // ONLY user-initiated closure that isn't approve/reject.
      await apiJson("POST", "/api/security/permission/cancel", {
        request_id: requestId,
      });
      pendingRequests.value = pendingRequests.value.filter(
        (r) => r.request_id !== requestId,
      );
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    }
  }

  /**
   * Phase 2 (plan §P5): cancel every pending request originating from a
   * specific pid — user "I don't want this Agent to continue" batch action.
   * Best-effort; on failure the error is surfaced via `error.value` and the
   * caller should refetch `/pending` to reconcile.
   */
  async function cancelPendingForPid(pid: number): Promise<void> {
    if (!Number.isFinite(pid) || pid <= 0) return;
    error.value = null;
    try {
      await apiJson("POST", "/api/security/permission/cancel", { pid });
      // Local prune — the wire schema uses `pid` on the pending DTO too
      // (backend adds it in Phase 2 via P3 persistence). Fall back to a
      // refetch if the field is not present.
      const hadPid = pendingRequests.value.some(
        (r) => (r as unknown as { pid?: number }).pid === pid,
      );
      if (hadPid) {
        pendingRequests.value = pendingRequests.value.filter(
          (r) => (r as unknown as { pid?: number }).pid !== pid,
        );
      } else {
        // Backend has pid but our types haven't; reconcile.
        await fetchPending();
      }
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    }
  }

  /**
   * Phase 2 (plan §P5): emergency "cancel all pending" — used from the
   * dialog's batch bar and from the orphan-cleanup affordance after a
   * service restart. Backend body: `{ cancel_all: true }`.
   */
  async function cancelAllPending(): Promise<void> {
    error.value = null;
    try {
      await apiJson("POST", "/api/security/permission/cancel", {
        cancel_all: true,
      });
      pendingRequests.value = [];
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    }
  }

  /**
   * Phase 2 (plan §P3): pull pending permission requests that survived a
   * service restart from the persistence DB. This is a thin alias over
   * `fetchPending` — kept as a named entry point so callers reading the
   * source can trace back to the "orphan surface after restart" flow. The
   * DB-restored rows carry a `boot_id` different from the current one, and
   * `usePermissionDialog.enqueue` will tag them with `is_orphan = true`.
   *
   * Best-effort; degrades to the same error handling as `fetchPending`.
   */
  async function fetchPendingOnStartup(): Promise<void> {
    await fetchPending();
  }

  /**
   * List sandbox grants scoped to a single subject.
   *
   * `GET /api/security/path-grants` REQUIRES `subject_kind`
   * (`user` | `preset` | `system`) and `subject_identifier` query
   * params — the backend repository only supports `list_for_subject`,
   * there is NO list-all endpoint. The canonical process-level subject
   * created by the dynamic-authorization bridge is
   * `system` / `ai_coding` (see `apps/api/_permission_bridge.py`), so
   * that is the default.
   */
  async function fetchGrants(
    subjectKind: "user" | "preset" | "system" = "system",
    subjectIdentifier = "ai_coding",
  ): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const res = await apiJson<GrantsListResponse>("GET", "/api/security/path-grants", undefined, {
        query: { subject_kind: subjectKind, subject_identifier: subjectIdentifier },
      });
      grants.value = res.grants ?? [];
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    } finally {
      loading.value = false;
    }
  }

  async function revokeGrant(grantId: string): Promise<void> {
    error.value = null;
    try {
      await apiJson("DELETE", `/api/security/path-grants/${grantId}`);
      grants.value = grants.value.filter((g) => g.grant_id !== grantId);
      processGrants.value = processGrants.value.filter((g) => g.grant_id !== grantId);
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    }
  }

  /**
   * Refresh the process-level grant count for the Overview tile.
   *
   * Uses the canonical `system` / `ai_coding` subject created by the
   * dynamic-authorization bridge. Failures degrade to an empty list
   * (count 0) rather than surfacing an error banner on the dashboard.
   */
  async function fetchProcessGrants(): Promise<void> {
    try {
      const res = await apiJson<GrantsListResponse>("GET", "/api/security/path-grants", undefined, {
        query: { subject_kind: "system", subject_identifier: "ai_coding" },
      });
      processGrants.value = res.grants ?? [];
    } catch {
      processGrants.value = [];
    }
  }

  /**
   * Refresh the session-level grant list for the Overview tile.
   *
   * V2 stores per-session grants under `subject_kind="user"` with the
   * identifier = coding_session_id (`_channel_grant_bridge`). When a
   * `codingSessionId` is supplied, query that subject; otherwise clear
   * the list (no active coding session = 0 session grants, matching V1).
   * Failures degrade to an empty list.
   */
  async function fetchSessionGrants(codingSessionId?: string): Promise<void> {
    const sid = (codingSessionId ?? "").trim();
    if (sid === "") {
      sessionGrants.value = [];
      return;
    }
    try {
      const res = await apiJson<GrantsListResponse>("GET", "/api/security/path-grants", undefined, {
        query: { subject_kind: "user", subject_identifier: sid },
      });
      sessionGrants.value = res.grants ?? [];
    } catch {
      sessionGrants.value = [];
    }
  }

  async function fetchAudit(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const res = await apiJson<AuditRecentResponse>("GET", "/api/security/audit/recent");
      auditEntries.value = res.entries;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    } finally {
      loading.value = false;
    }
  }

  /**
   * Poll FileGuard health (Overview header pill + down/test_mode banner).
   *
   * V1 parity (`SecurityConfigPanel.js:239-249`): failures degrade to
   * `unknown` rather than surfacing an error banner, so a transient
   * health-endpoint hiccup never masks the rest of the panel.
   */
  async function fetchHealth(): Promise<void> {
    try {
      health.value = await apiJson<SecurityHealthResponse>("GET", "/api/security/health");
    } catch {
      health.value = { ...HEALTH_UNKNOWN };
    }
  }

  /** Load the built-in policy-template catalogue (`GET /templates`). */
  async function fetchPolicyTemplates(): Promise<void> {
    loadingTemplates.value = true;
    try {
      const res = await apiJson<TemplatesListResponse>("GET", "/api/security/templates");
      policyTemplates.value = res.templates ?? [];
    } catch {
      policyTemplates.value = [];
    } finally {
      loadingTemplates.value = false;
    }
  }

  /**
   * Apply a built-in template (`POST /templates`) and refresh the policy.
   *
   * V1 parity (`SecurityConfigPanel.js:794-811`): replaces the rule set
   * through the locked UpdatePolicyUseCase, then re-reads the policy so
   * the Allow-Lists tab + Overview reflect the new rules.
   */
  async function applyTemplate(templateId: string): Promise<void> {
    applyingTemplate.value = templateId;
    error.value = null;
    try {
      const res = await apiJson<ApplyTemplateResponse>(
        "POST",
        "/api/security/templates",
        { template: templateId },
      );
      policy.value = res.policy;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    } finally {
      applyingTemplate.value = null;
    }
  }

  /** Load discovered skill capabilities (`GET /skill-discovery`). */
  async function fetchSkillDiscovery(): Promise<void> {
    try {
      const res = await apiJson<SkillDiscoveryResponse>("GET", "/api/security/skill-discovery");
      discoveredSkills.value = res.skills ?? [];
    } catch {
      discoveredSkills.value = [];
    }
  }

  /**
   * Persist one or more Overview policy toggles via the locked
   * `PUT /api/security/policy` contract.
   *
   * The toggles are tail-appended onto the policy wire shape; we send
   * them alongside the CURRENT rule set so the rules CRUD payload is
   * preserved byte-for-byte (a toggle change must never mutate rules).
   */
  async function updatePolicyToggles(patch: PolicyToggles): Promise<void> {
    error.value = null;
    const current = policy.value;
    const rules: PolicyRuleDTO[] = current?.rules ?? [];
    try {
      policy.value = await apiJson<PolicyResponse>("PUT", "/api/security/policy", {
        rules,
        reboot_reason: "overview toggle changed",
        ...patch,
      });
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
      // Re-read authoritative state so the toggle UI reverts on failure.
      await fetchPolicy();
    }
  }

  /**
   * Set the security master switch (3c switch-tree §6.4) via the NEW
   * `PUT /api/security/mode` contract.
   *
   * This is a NEW control — it does NOT reuse `updatePolicyToggles`
   * (whose `mode` value domain is the locked `enforce | audit_only`
   * run-mode sub-switch). The PUT response is the authoritative master
   * mode; we cache it in {@link securityMode}. Because the backend keeps
   * `disabled ⟺ policy.enabled` in lock-step (single truth source), we
   * re-read the policy afterwards so the FileGuard master-switch toggle
   * and run-mode sub-control reflect the new effective state without a
   * second manual refresh.
   */
  async function setSecurityMode(mode: SecurityMode): Promise<void> {
    error.value = null;
    try {
      const res = await apiJson<SecurityModeResponse, SetSecurityModeRequest>(
        "PUT",
        "/api/security/mode",
        { mode },
      );
      securityMode.value = res.mode;
      // Reconcile the locked policy toggles (`enabled`) with the freshly-set
      // master switch — `disabled` flips `enabled` off on the backend.
      await fetchPolicy();
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
      await fetchPolicy();
    }
  }

  return {
    // state
    policy,
    pendingRequests,
    grants,
    processGrants,
    sessionGrants,
    auditEntries,
    loading,
    error,
    health,
    policyTemplates,
    loadingTemplates,
    applyingTemplate,
    discoveredSkills,
    securityMode,
    // actions
    fetchPolicy,
    fetchPending,
    approvePermission,
    rejectPermission,
    cancelPermission,
    cancelPendingForPid,
    cancelAllPending,
    fetchPendingOnStartup,
    fetchGrants,
    fetchProcessGrants,
    fetchSessionGrants,
    revokeGrant,
    fetchAudit,
    fetchHealth,
    fetchPolicyTemplates,
    applyTemplate,
    fetchSkillDiscovery,
    updatePolicyToggles,
    setSecurityMode,
  };
});
