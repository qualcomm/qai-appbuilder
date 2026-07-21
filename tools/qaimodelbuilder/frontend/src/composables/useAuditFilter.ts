// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useAuditFilter` — Audit log multi-dimension client-side filter.
 *
 * Extracts the audit-tab filter state + matcher pure logic out of
 * `AuditLogPanel.vue` so the panel only owns IO/template concerns.
 *
 * V1 parity: V1 `SecurityConfigPanel.js` audit tab filters on flat fields
 * decision/op/channel + a path search with substring/wildcard/regex modes.
 * V2's structured `_AuditEntryDTO` exposes the same dimensions via
 * `decision` / `resource.kind` / `subject.kind` / `resource.identifier`,
 * so the filter is implemented client-side over the real V2 fields:
 *   • decision  → entry.decision           (allow | deny)
 *   • op        → entry.resource.kind       (path|skill|network|exec|dep)
 *   • source    → entry.subject.kind        (user|preset|system)
 *   • origin    → entry.subject.identifier  (in-process|native|other)  [3-A]
 *   • path/text → resource.identifier etc.  (substring|wildcard|regex)
 *
 * V2 audit decision is architecturally narrowed to allow/deny:
 * V1's ASK is mapped to DENY at check_permission (headless has no prompt UI),
 * and V1's INFO (cmdline parse fail-open) is replaced by V2's safer DENY.
 * Hence no ask/info filter options — they would never match any record.
 *
 * The composable returns the four filter refs, the path-mode ref, the
 * `pathFilterInvalid` flag (true when a regex fails to compile), and a
 * `filteredEntries` computed bound to the entries ref the caller passes
 * in. Pure functions are kept module-local so they remain trivially
 * testable in isolation.
 */
import { computed, ref, type ComputedRef, type Ref } from "vue";

// ─── Public types ────────────────────────────────────────────────────────────

export type AuditPathMode = "substring" | "wildcard" | "regex";
export type AuditDecisionFilter = "" | "allow" | "deny";
export type AuditResourceKind = "path" | "skill" | "network" | "exec" | "dep";
export type AuditSubjectKind = "user" | "preset" | "system";
export type AuditOpFilter = "" | AuditResourceKind;
export type AuditSourceFilter = "" | AuditSubjectKind;
/**
 * Origin classification (SEC-ENHANCE-AUDITUX 3-A). Both in-process tool
 * events and native sub-process events carry `subject.kind === "system"`,
 * so the `filterSource` (by kind) dimension CANNOT tell them apart. This
 * additive dimension classifies by the canonical `subject.identifier`:
 *   • in-process → subject.identifier === "ai_coding.tool"
 *   • native     → subject.identifier === "native.file_guard"
 *   • other      → anything else (users / presets / other system subjects)
 */
export type AuditOrigin = "in-process" | "native" | "other";
export type AuditOriginFilter = "" | AuditOrigin;

/** Canonical `subject.identifier` values emitted by the two tool lanes. */
const ORIGIN_IN_PROCESS_IDENTIFIER = "ai_coding.tool";
const ORIGIN_NATIVE_IDENTIFIER = "native.file_guard";

/**
 * Classify an entry's origin from its `subject.identifier`. Pure + exported
 * so both the filter predicate and the panel's per-row badge share one
 * source of truth (no drift between filter and display).
 */
export function classifyAuditOrigin(subjectIdentifier: string): AuditOrigin {
  if (subjectIdentifier === ORIGIN_IN_PROCESS_IDENTIFIER) return "in-process";
  if (subjectIdentifier === ORIGIN_NATIVE_IDENTIFIER) return "native";
  return "other";
}
/** Origin-channel filter (V1 parity: web / wechat / feishu / cli / background). */
export type AuditChannelFilter =
  | ""
  | "web"
  | "wechat"
  | "feishu"
  | "cli"
  | "background";

/**
 * Minimal entry shape this composable depends on. The host panel may pass
 * any superset (the real V2 `_AuditEntryDTO` includes more fields like
 * `audit_id` / `occurred_at` — those are immaterial to filtering).
 */
export interface AuditFilterableEntry {
  decision: "allow" | "deny";
  resource: { kind: AuditResourceKind; identifier: string };
  subject: { kind: AuditSubjectKind; identifier: string };
  note: string;
  /** Origin channel (V1 parity); null/undefined for system actions. */
  channel?: string | null;
  // ── Tail-appended native-actor metadata (SEC-ENHANCE-AUDITUX 3-B). ────────
  // OPTIONAL: undefined on old rows and on non-native (in-process) events.
  // The backend appends these to the audit DTO for native sub-process events.
  /** Concrete operation (read|write|delete|exec); preferred over resource.kind. */
  op?: string;
  /** Absolute image path of the native process that triggered the event. */
  process_path?: string;
  /** Full command line of the native process. */
  command_line?: string;
  /** PID of the acting native process. */
  actor_pid?: number | null;
  /** PID of the acting native process's parent. */
  actor_parent_pid?: number | null;
}

// ─── Pure helpers (module-local, easily unit-tested) ─────────────────────────

/**
 * Build a matcher honoring the selected mode. Returns `null` when the
 * raw text is empty (→ caller skips text filtering) or the regex fails
 * to compile (→ caller surfaces an "invalid" UI state via
 * `pathFilterInvalid`).
 */
export function buildAuditPathMatcher(
  raw: string,
  mode: AuditPathMode,
): { matcher: ((s: string) => boolean) | null; invalid: boolean } {
  const trimmed = raw.trim();
  if (trimmed === "") return { matcher: null, invalid: false };

  if (mode === "regex") {
    try {
      const re = new RegExp(trimmed, "i");
      return { matcher: (s: string) => re.test(s), invalid: false };
    } catch {
      return { matcher: null, invalid: true };
    }
  }

  if (mode === "wildcard") {
    // Escape regex specials, then `*` → `.*` and `?` → `.`.
    const escaped = trimmed.replace(/[.+^${}()|[\]\\]/g, "\\$&");
    const pattern = escaped.replace(/\*/g, ".*").replace(/\?/g, ".");
    try {
      const re = new RegExp(pattern, "i");
      return { matcher: (s: string) => re.test(s), invalid: false };
    } catch {
      return { matcher: null, invalid: false };
    }
  }

  // substring (case-insensitive)
  const lower = trimmed.toLowerCase();
  return { matcher: (s: string) => s.toLowerCase().includes(lower), invalid: false };
}

// ─── Composable ──────────────────────────────────────────────────────────────

export interface UseAuditFilterReturn<E extends AuditFilterableEntry> {
  // filter state
  filterDecision: Ref<AuditDecisionFilter>;
  filterOp: Ref<AuditOpFilter>;
  filterSource: Ref<AuditSourceFilter>;
  /**
   * Origin filter (SEC-ENHANCE-AUDITUX 3-A) — additive dimension that
   * separates in-process tool events from native sub-process events by
   * `subject.identifier` (both share `subject.kind === "system"`).
   */
  filterOrigin: Ref<AuditOriginFilter>;
  filterChannel: Ref<AuditChannelFilter>;
  filterText: Ref<string>;
  pathMode: Ref<AuditPathMode>;
  /** `true` when the current `filterText` is an invalid regex. */
  pathFilterInvalid: Ref<boolean>;
  /** Entries surviving all filter dimensions. */
  filteredEntries: ComputedRef<E[]>;
}

/**
 * Wire up filter refs against an entries source. The caller owns the
 * source ref (typically populated from `GET /api/security/audit/recent`).
 */
export function useAuditFilter<E extends AuditFilterableEntry>(
  entries: Ref<E[]>,
): UseAuditFilterReturn<E> {
  const filterDecision = ref<AuditDecisionFilter>("");
  const filterOp = ref<AuditOpFilter>("");
  const filterSource = ref<AuditSourceFilter>("");
  const filterOrigin = ref<AuditOriginFilter>("");
  const filterChannel = ref<AuditChannelFilter>("");
  const filterText = ref("");
  const pathMode = ref<AuditPathMode>("substring");
  const pathFilterInvalid = ref(false);

  const filteredEntries = computed<E[]>(() => {
    const { matcher, invalid } = buildAuditPathMatcher(filterText.value, pathMode.value);
    pathFilterInvalid.value = invalid;

    return entries.value.filter((e) => {
      if (filterDecision.value !== "" && e.decision !== filterDecision.value) return false;
      if (filterOp.value !== "" && e.resource.kind !== filterOp.value) return false;
      if (filterSource.value !== "" && e.subject.kind !== filterSource.value) return false;
      if (
        filterOrigin.value !== "" &&
        classifyAuditOrigin(e.subject.identifier) !== filterOrigin.value
      )
        return false;
      if (filterChannel.value !== "" && (e.channel ?? "") !== filterChannel.value) return false;
      if (matcher !== null) {
        const haystack = `${e.resource.identifier} ${e.resource.kind} ${e.subject.identifier} ${e.decision} ${e.note} ${e.channel ?? ""}`;
        if (!matcher(haystack)) return false;
      }
      return true;
    });
  });

  return {
    filterDecision,
    filterOp,
    filterSource,
    filterOrigin,
    filterChannel,
    filterText,
    pathMode,
    pathFilterInvalid,
    filteredEntries,
  };
}
