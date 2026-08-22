// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Mode-template store — collaboration modes ("怎么协作": 讨论/评审/辩论/实施/custom).
 *
 * Thin CRUD client for the conversation-INDEPENDENT mode-template library (the
 * third tier of the three-tier template system, §26/§27):
 *
 *   GET    /api/chat/mode-templates
 *   POST   /api/chat/mode-templates
 *   PATCH  /api/chat/mode-templates/{id}
 *   DELETE /api/chat/mode-templates/{id}
 *   POST   /api/chat/mode-templates/{id}/apply   (set conversation selected_mode_id)
 *
 * A mode is identity / framing / tool_policy(allow|deny) / flow_policy (the V1
 * subset of §26.1; execution-time confirmation / sandbox are out of V1 scope —
 * the discussion runtime has no such gate). Built-in presets (``is_builtin``)
 * are factory-seeded and read-only. The policy blobs are passed through as
 * opaque records (the back-end domain owns their shape); the UI mainly surfaces
 * name/description/framing.
 *
 * PURE V2 enhancement (V1 has no multi-Agent discussion).
 */
import { defineStore } from "pinia";
import { ref } from "vue";
import { apiJson } from "@/api";

// ---------------------------------------------------------------------------
// Wire shapes (snake_case — backend contract)
// ---------------------------------------------------------------------------

export type ToolPolicyState = "deny" | "allow";

export interface ModeToolPolicyWire {
  default?: ToolPolicyState;
  tools?: Record<string, ToolPolicyState>;
}

export interface ModeFlowPolicyWire {
  speaker_strategy?: "manager" | "round_robin";
  max_rounds?: number;
  judge_enabled?: boolean;
  allow_mode_switch?: boolean;
  /** Mandatory "system model" for the mode — the cloud model id used to drive
   *  this collaboration mode's orchestration. Nested in flow_policy (snake_case
   *  key) per the backend ModeFlowPolicy contract. */
  system_model_id?: string;
}

export interface ModeLintIssueWire {
  severity: string;
  code: string;
  message: string;
}

/**
 * Meeting-room soft constraints (decisions 3+9, §26.8). Either field `null` =
 * that constraint is not enabled; the whole object `null` = no constraint.
 * SOFT only — fed into the speaker prompt, never enforced at runtime.
 */
export interface ModeHardConstraintsWire {
  max_chars_per_turn?: number | null;
  max_seconds_per_turn?: number | null;
}

interface ModeTemplateWire {
  id: string;
  name: string;
  description: string;
  framing: string;
  tool_policy: ModeToolPolicyWire;
  flow_policy: ModeFlowPolicyWire;
  hard_constraints?: ModeHardConstraintsWire | null;
  is_builtin: boolean;
  lint_issues?: ModeLintIssueWire[] | null;
  cloned_from_id?: string | null;
  created_at: string;
  updated_at: string;
  /** Per-locale i18n maps for built-in presets (migration 056); null/absent
   *  for custom rows → fall back to the single-language fields above. */
  name_i18n?: Record<string, string> | null;
  description_i18n?: Record<string, string> | null;
  framing_i18n?: Record<string, string> | null;
}

interface ModeTemplateListWire {
  items?: ModeTemplateWire[] | null;
}

interface ApplyResponseWire {
  conversation_id: string;
  mode_id: string;
  mode_name: string;
  selection_policy: string;
}

interface UsageResponseWire {
  mode_id: string;
  conversation_count: number;
}

// ---------------------------------------------------------------------------
// View models (camelCase)
// ---------------------------------------------------------------------------

export interface ModeLintIssueView {
  severity: string;
  code: string;
  message: string;
}

export interface ModeTemplateView {
  id: string;
  name: string;
  description: string;
  framing: string;
  toolPolicy: ModeToolPolicyWire;
  flowPolicy: ModeFlowPolicyWire;
  /** Mandatory mode "system model" cloud id, projected out of flow_policy for
   *  convenient binding; "" when unset. */
  systemModel: string;
  /** Meeting-room soft constraints (decisions 3+9); null = none enabled. */
  hardConstraints: ModeHardConstraintsWire | null;
  isBuiltin: boolean;
  /** Advisory framing↔tool-policy soft conflicts (§26.8); [] when clean. */
  lintIssues: ModeLintIssueView[];
  /** Source template id when this is a clone (esp. a clone of a factory preset);
   *  "" / undefined = not a clone. Reset is only meaningful when set. */
  clonedFromId?: string;
  /** Per-locale i18n maps for built-in presets; undefined for custom rows.
   *  Consumed by useTemplateI18n at the display layer. */
  nameI18n?: Record<string, string>;
  descriptionI18n?: Record<string, string>;
  framingI18n?: Record<string, string>;
}

/** Body for create / update (id is route/response only). */
export interface ModeTemplateInput {
  name: string;
  description: string;
  framing: string;
  toolPolicy?: ModeToolPolicyWire;
  flowPolicy?: ModeFlowPolicyWire;
  hardConstraints?: ModeHardConstraintsWire | null;
}

// ---------------------------------------------------------------------------
// Wire ↔ view-model mappers
// ---------------------------------------------------------------------------

function wireToView(w: ModeTemplateWire): ModeTemplateView {
  return {
    id: w.id,
    name: w.name,
    description: w.description,
    framing: w.framing ?? "",
    toolPolicy: w.tool_policy ?? {},
    flowPolicy: w.flow_policy ?? {},
    systemModel: w.flow_policy?.system_model_id ?? "",
    hardConstraints: w.hard_constraints ?? null,
    isBuiltin: w.is_builtin === true,
    lintIssues: Array.isArray(w.lint_issues)
      ? w.lint_issues.map((i) => ({
          severity: i.severity,
          code: i.code,
          message: i.message,
        }))
      : [],
    ...(w.cloned_from_id != null && w.cloned_from_id !== ""
      ? { clonedFromId: w.cloned_from_id }
      : {}),
    ...(w.name_i18n != null ? { nameI18n: w.name_i18n } : {}),
    ...(w.description_i18n != null
      ? { descriptionI18n: w.description_i18n }
      : {}),
    ...(w.framing_i18n != null ? { framingI18n: w.framing_i18n } : {}),
  };
}

function inputToWire(input: ModeTemplateInput): Record<string, unknown> {
  return {
    name: input.name,
    description: input.description,
    framing: input.framing,
    ...(input.toolPolicy !== undefined ? { tool_policy: input.toolPolicy } : {}),
    ...(input.flowPolicy !== undefined ? { flow_policy: input.flowPolicy } : {}),
    ...(input.hardConstraints !== undefined
      ? { hard_constraints: input.hardConstraints }
      : {}),
  };
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const useModeTemplateStore = defineStore("modeTemplate", () => {
  const templates = ref<ModeTemplateView[]>([]);
  const loaded = ref(false);

  async function fetchAll(): Promise<ModeTemplateView[]> {
    const resp = await apiJson<ModeTemplateListWire>(
      "GET",
      "/api/chat/mode-templates",
    );
    const items = Array.isArray(resp?.items) ? resp.items : [];
    templates.value = items.map(wireToView);
    loaded.value = true;
    return templates.value;
  }

  async function create(input: ModeTemplateInput): Promise<ModeTemplateView> {
    const w = await apiJson<ModeTemplateWire>(
      "POST",
      "/api/chat/mode-templates",
      inputToWire(input),
    );
    const view = wireToView(w);
    templates.value = [...templates.value, view];
    return view;
  }

  async function update(
    id: string,
    input: ModeTemplateInput,
  ): Promise<ModeTemplateView> {
    const w = await apiJson<ModeTemplateWire>(
      "PATCH",
      `/api/chat/mode-templates/${encodeURIComponent(id)}`,
      inputToWire(input),
    );
    const view = wireToView(w);
    templates.value = templates.value.map((tpl) => (tpl.id === id ? view : tpl));
    return view;
  }

  async function remove(id: string): Promise<void> {
    await apiJson("DELETE", `/api/chat/mode-templates/${encodeURIComponent(id)}`);
    templates.value = templates.value.filter((tpl) => tpl.id !== id);
  }

  /** Select a mode for a conversation. Returns the applied mode name. */
  async function applyToConversation(
    id: string,
    conversationId: string,
    selectionPolicy = "manual",
  ): Promise<string> {
    const resp = await apiJson<
      ApplyResponseWire,
      { conversation_id: string; selection_policy: string }
    >("POST", `/api/chat/mode-templates/${encodeURIComponent(id)}/apply`, {
      conversation_id: conversationId,
      selection_policy: selectionPolicy,
    });
    return resp.mode_name;
  }

  /**
   * Count conversations currently selecting this mode (decision 7). The
   * delete-confirm dialog uses it to warn the user how many conversations will
   * be reverted to the sentinel ("跟随默认") before they delete the mode.
   */
  async function usage(id: string): Promise<number> {
    const resp = await apiJson<UsageResponseWire>(
      "GET",
      `/api/chat/mode-templates/${encodeURIComponent(id)}/usage`,
    );
    return typeof resp?.conversation_count === "number"
      ? resp.conversation_count
      : 0;
  }

  /** Clone any template (factory preset or own) into a NEW non-builtin copy
   *  (records cloned_from_id server-side). Returns the new copy view. */
  async function clone(id: string): Promise<ModeTemplateView> {
    const w = await apiJson<ModeTemplateWire>(
      "POST",
      `/api/chat/mode-templates/${encodeURIComponent(id)}/clone`,
    );
    const view = wireToView(w);
    templates.value = [...templates.value, view];
    return view;
  }

  /** Reset a "clone of a factory preset" copy back to its source content (the
   *  copy id is preserved). Returns the reset view. */
  async function reset(id: string): Promise<ModeTemplateView> {
    const w = await apiJson<ModeTemplateWire>(
      "POST",
      `/api/chat/mode-templates/${encodeURIComponent(id)}/reset`,
    );
    const view = wireToView(w);
    templates.value = templates.value.map((tpl) => (tpl.id === id ? view : tpl));
    return view;
  }

  return {
    templates,
    loaded,
    fetchAll,
    create,
    update,
    remove,
    applyToConversation,
    usage,
    clone,
    reset,
  };
});
