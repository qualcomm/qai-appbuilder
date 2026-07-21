// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Implementation-plan store (DISC-1 二期 §22.9 control plane).
 *
 * Thin CRUD client for the conversation-scoped implementation plan. Wraps the
 * two backend routes (contract verbatim, snake_case):
 *
 *   GET   /api/chat/conversations/{id}/implementation
 *   PATCH /api/chat/conversations/{id}/implementation   body { items: [...] }
 *
 * Mirrors `stores/discussion.ts` in spirit: the store is STATELESS beyond the
 * HTTP calls + wire↔view-model mapping. The authoritative reactive copy of a
 * tab's implementation run lives on `chatTabs.tab.implementation` (so the
 * ImplementationPanel, the SSE frame handlers, and `useImplementation` all read
 * one place). `useImplementation` is the composable that binds this store to the
 * active tab; this store only owns the wire shapes + HTTP calls.
 *
 * Execution control (start / pause / resume / stop) is NOT a route here — it is
 * driven by sending a localized control message through the ordinary chat send
 * path (control router), so this store never grows a second control plane.
 *
 * The PATCH route does a WHOLE-ARRAY merge by item id: an id present in the
 * body is updated (existing) / created (new); an existing id ABSENT from the
 * body is deleted. The backend ignores any non-editable fields the UI may send
 * (only assigned_role / title / description / acceptance_criteria / status
 * (pending↔skipped) + add/remove take effect). Callers therefore must send the
 * full set of item ids they want to keep.
 */
import { defineStore } from "pinia";
import { apiJson } from "@/api";
import {
  DEFAULT_IMPLEMENTATION_STATE,
  type ImplementationItemVM,
  type TabImplementationState,
} from "./_chatTabsTypes";

// ---------------------------------------------------------------------------
// Wire shapes (backend contract — snake_case)
// ---------------------------------------------------------------------------

/** One feature item on the wire (`ImplementationItemBody`). Mirrors the backend
 *  field set verbatim; the UI only ever MUTATES a subset (see `ItemPatchWire`),
 *  but reads the full shape. */
export interface ImplementationItemWire {
  id: string;
  title: string;
  description?: string | null;
  acceptance_criteria?: string[] | null;
  suggested_role?: string | null;
  assigned_role?: string | null;
  status?: string | null;
  result_summary?: string | null;
  depends_on?: string[] | null;
  source_refs?: string[] | null;
  attempt_count?: number | null;
  started_at?: string | null;
  finished_at?: string | null;
  last_error?: string | null;
  /** DISC-1 完成判定 B — per-item verification command. */
  verify_command?: string | null;
}

/** The full plan on the wire (`ImplementationPlanBody`). When no plan exists the
 *  backend returns an empty shell (`phase: "none"`, `items: []`, `version: 1`)
 *  rather than 404. */
export interface ImplementationPlanWire {
  version?: number | null;
  phase?: string | null;
  run_id?: string | null;
  current_item?: string | null;
  items?: ImplementationItemWire[] | null;
  created_at?: string | null;
  updated_at?: string | null;
  last_error?: string | null;
  stopped_by_user?: boolean | null;
  paused_at?: string | null;
}

/** The subset of item fields the UI is allowed to mutate via PATCH. The backend
 *  ignores everything else; `id` is always required (merge key). `status` is
 *  only honoured for `pending ↔ skipped` transitions. */
export interface ItemPatchWire {
  id: string;
  title?: string;
  description?: string;
  acceptance_criteria?: string[];
  assigned_role?: string | null;
  status?: string;
  /** DISC-1 完成判定 B — per-item verification command. */
  verify_command?: string | null;
}

// ---------------------------------------------------------------------------
// Wire ↔ view-model mappers
// ---------------------------------------------------------------------------

/** Map one wire item to the control-plane view model (camelCase). Holds only the
 *  fields the progress row needs (§22.9 — never the full output / diff). */
export function wireToItem(w: ImplementationItemWire): ImplementationItemVM {
  return {
    id: w.id,
    title: typeof w.title === "string" ? w.title : "",
    status: typeof w.status === "string" && w.status !== "" ? w.status : "pending",
    assignedRole:
      typeof w.assigned_role === "string" && w.assigned_role !== ""
        ? w.assigned_role
        : null,
    suggestedRole:
      typeof w.suggested_role === "string" && w.suggested_role !== ""
        ? w.suggested_role
        : null,
    resultSummary:
      typeof w.result_summary === "string" && w.result_summary !== ""
        ? w.result_summary
        : null,
    lastError:
      typeof w.last_error === "string" && w.last_error !== ""
        ? w.last_error
        : null,
    description: typeof w.description === "string" ? w.description : "",
    acceptanceCriteria: Array.isArray(w.acceptance_criteria)
      ? w.acceptance_criteria.filter((c): c is string => typeof c === "string")
      : [],
    verifyCommand: typeof w.verify_command === "string" ? w.verify_command : "",
    dependsOn: Array.isArray(w.depends_on)
      ? w.depends_on.filter((d): d is string => typeof d === "string")
      : [],
    attemptCount:
      typeof w.attempt_count === "number" && Number.isFinite(w.attempt_count)
        ? w.attempt_count
        : 0,
  };
}

/** Map the full plan wire to the per-tab implementation state (camelCase). An
 *  empty / absent plan maps to the neutral idle state (`phase: "none"`). */
export function wireToState(w: ImplementationPlanWire | null): TabImplementationState {
  if (w === null || typeof w !== "object") {
    return { ...DEFAULT_IMPLEMENTATION_STATE };
  }
  const items = Array.isArray(w.items) ? w.items : [];
  return {
    phase: typeof w.phase === "string" && w.phase !== "" ? w.phase : "none",
    runId: typeof w.run_id === "string" && w.run_id !== "" ? w.run_id : null,
    currentItem:
      typeof w.current_item === "string" && w.current_item !== ""
        ? w.current_item
        : null,
    items: items.map(wireToItem),
  };
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const useImplementationStore = defineStore("implementation", () => {
  /** Fetch the implementation plan for a conversation. The backend returns an
   *  empty shell (not 404) when no plan exists, so the caller always gets a
   *  well-formed wire object. */
  async function fetchImplementationPlan(
    conversationId: string,
  ): Promise<ImplementationPlanWire> {
    return await apiJson<ImplementationPlanWire>(
      "GET",
      `/api/chat/conversations/${encodeURIComponent(conversationId)}/implementation`,
    );
  }

  /** PATCH the whole item array (merge-by-id: present id = upsert, absent id =
   *  delete). Returns the updated plan. Only editable fields take effect
   *  backend-side. */
  async function updateImplementationPlan(
    conversationId: string,
    items: ItemPatchWire[],
  ): Promise<ImplementationPlanWire> {
    return await apiJson<ImplementationPlanWire>(
      "PATCH",
      `/api/chat/conversations/${encodeURIComponent(conversationId)}/implementation`,
      { items },
    );
  }

  return {
    fetchImplementationPlan,
    updateImplementationPlan,
  };
});
