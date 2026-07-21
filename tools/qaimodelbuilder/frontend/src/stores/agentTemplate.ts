// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Agent-template store — reusable single-role "agents".
 *
 * Thin CRUD client for the conversation-INDEPENDENT agent-template library
 * (the smallest reusable unit in the three-tier template system, §27):
 *
 *   GET    /api/chat/agent-templates
 *   POST   /api/chat/agent-templates
 *   PATCH  /api/chat/agent-templates/{id}
 *   DELETE /api/chat/agent-templates/{id}
 *   POST   /api/chat/agent-templates/{id}/apply   (instantiate into a conversation)
 *
 * An agent template is a named definition of a SINGLE role (display_name /
 * model_id / persona / config) a user can preview + import into any
 * conversation, so a frequently-used role need not be re-typed. Built-in
 * presets (``is_builtin``) are factory-seeded and read-only.
 *
 * PURE V2 enhancement (V1 has no multi-Agent discussion). The store owns the
 * wire shapes + HTTP only; it maps snake_case wire ↔ camelCase view models.
 */
import { defineStore } from "pinia";
import { ref } from "vue";
import { apiJson } from "@/api";

// ---------------------------------------------------------------------------
// Wire shapes (snake_case — backend contract)
// ---------------------------------------------------------------------------

interface AgentConfigWire {
  allowed_tools?: string[] | null;
  enabled_skills?: string[] | null;
  color?: number | string | null;
}

interface AgentTemplateWire {
  id: string;
  name: string;
  description: string;
  display_name: string;
  model_id?: string | null;
  persona?: string | null;
  config?: AgentConfigWire | null;
  is_builtin: boolean;
  cloned_from_id?: string | null;
  created_at: string;
  updated_at: string;
  /** Per-locale i18n maps for built-in presets (migration 056); null/absent
   *  for custom rows → fall back to the single-language fields above. */
  name_i18n?: Record<string, string> | null;
  description_i18n?: Record<string, string> | null;
  display_name_i18n?: Record<string, string> | null;
  persona_i18n?: Record<string, string> | null;
}

interface AgentTemplateListWire {
  items?: AgentTemplateWire[] | null;
}

interface ApplyResponseWire {
  conversation_id: string;
  participant_id: string;
}

// ---------------------------------------------------------------------------
// View models (camelCase)
// ---------------------------------------------------------------------------

export interface AgentTemplateView {
  id: string;
  name: string;
  description: string;
  displayName: string;
  modelId?: string;
  persona?: string;
  allowedTools: string[];
  enabledSkills: string[];
  color?: number;
  isBuiltin: boolean;
  /** Source template id when this is a clone (esp. a clone of a factory preset);
   *  "" / undefined = not a clone. Reset is only meaningful when set. */
  clonedFromId?: string;
  /** Per-locale i18n maps for built-in presets; undefined for custom rows.
   *  Consumed by useTemplateI18n at the display layer to localise built-in
   *  text without duplicating translations into the frontend locale files. */
  nameI18n?: Record<string, string>;
  descriptionI18n?: Record<string, string>;
  displayNameI18n?: Record<string, string>;
  personaI18n?: Record<string, string>;
}

/** Body for create / update (id is route/response only). */
export interface AgentTemplateInput {
  name: string;
  description: string;
  displayName: string;
  modelId?: string;
  persona?: string;
  allowedTools: string[];
  enabledSkills: string[];
  color?: number;
}

// ---------------------------------------------------------------------------
// Wire ↔ view-model mappers
// ---------------------------------------------------------------------------

function wireToTemplate(w: AgentTemplateWire): AgentTemplateView {
  return {
    id: w.id,
    name: w.name,
    description: w.description,
    displayName: w.display_name,
    ...(w.model_id != null && w.model_id !== "" ? { modelId: w.model_id } : {}),
    ...(w.persona != null && w.persona !== "" ? { persona: w.persona } : {}),
    allowedTools: Array.isArray(w.config?.allowed_tools)
      ? [...(w.config?.allowed_tools as string[])]
      : [],
    enabledSkills: Array.isArray(w.config?.enabled_skills)
      ? [...(w.config?.enabled_skills as string[])]
      : [],
    ...(typeof w.config?.color === "number" ? { color: w.config?.color } : {}),
    isBuiltin: w.is_builtin === true,
    ...(w.cloned_from_id != null && w.cloned_from_id !== ""
      ? { clonedFromId: w.cloned_from_id }
      : {}),
    ...(w.name_i18n != null ? { nameI18n: w.name_i18n } : {}),
    ...(w.description_i18n != null
      ? { descriptionI18n: w.description_i18n }
      : {}),
    ...(w.display_name_i18n != null
      ? { displayNameI18n: w.display_name_i18n }
      : {}),
    ...(w.persona_i18n != null ? { personaI18n: w.persona_i18n } : {}),
  };
}

function inputToWire(input: AgentTemplateInput): Record<string, unknown> {
  return {
    name: input.name,
    description: input.description,
    display_name: input.displayName,
    ...(input.modelId !== undefined && input.modelId !== ""
      ? { model_id: input.modelId }
      : {}),
    ...(input.persona !== undefined && input.persona !== ""
      ? { persona: input.persona }
      : {}),
    config: {
      allowed_tools: input.allowedTools,
      enabled_skills: input.enabledSkills,
      ...(input.color !== undefined ? { color: input.color } : {}),
    },
  };
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const useAgentTemplateStore = defineStore("agentTemplate", () => {
  /** Cached library (built-ins first, then user saved). */
  const templates = ref<AgentTemplateView[]>([]);
  const loaded = ref(false);

  async function fetchAll(): Promise<AgentTemplateView[]> {
    const resp = await apiJson<AgentTemplateListWire>(
      "GET",
      "/api/chat/agent-templates",
    );
    const items = Array.isArray(resp?.items) ? resp.items : [];
    templates.value = items.map(wireToTemplate);
    loaded.value = true;
    return templates.value;
  }

  async function create(input: AgentTemplateInput): Promise<AgentTemplateView> {
    const w = await apiJson<AgentTemplateWire>(
      "POST",
      "/api/chat/agent-templates",
      inputToWire(input),
    );
    const view = wireToTemplate(w);
    templates.value = [...templates.value, view];
    return view;
  }

  async function update(
    id: string,
    input: AgentTemplateInput,
  ): Promise<AgentTemplateView> {
    const w = await apiJson<AgentTemplateWire>(
      "PATCH",
      `/api/chat/agent-templates/${encodeURIComponent(id)}`,
      inputToWire(input),
    );
    const view = wireToTemplate(w);
    templates.value = templates.value.map((tpl) => (tpl.id === id ? view : tpl));
    return view;
  }

  async function remove(id: string): Promise<void> {
    await apiJson("DELETE", `/api/chat/agent-templates/${encodeURIComponent(id)}`);
    templates.value = templates.value.filter((tpl) => tpl.id !== id);
  }

  /** Apply (import) an agent template into a conversation as one named agent.
   *  Returns the created participant id. */
  async function applyToConversation(
    id: string,
    conversationId: string,
  ): Promise<string> {
    const resp = await apiJson<ApplyResponseWire, { conversation_id: string }>(
      "POST",
      `/api/chat/agent-templates/${encodeURIComponent(id)}/apply`,
      { conversation_id: conversationId },
    );
    return resp.participant_id;
  }

  /** Clone any template (factory preset or own) into a NEW non-builtin copy
   *  (records cloned_from_id server-side). Returns the new copy view. */
  async function clone(id: string): Promise<AgentTemplateView> {
    const w = await apiJson<AgentTemplateWire>(
      "POST",
      `/api/chat/agent-templates/${encodeURIComponent(id)}/clone`,
    );
    const view = wireToTemplate(w);
    templates.value = [...templates.value, view];
    return view;
  }

  /** Reset a "clone of a factory preset" copy back to its source content (the
   *  copy id is preserved). Returns the reset view. */
  async function reset(id: string): Promise<AgentTemplateView> {
    const w = await apiJson<AgentTemplateWire>(
      "POST",
      `/api/chat/agent-templates/${encodeURIComponent(id)}/reset`,
    );
    const view = wireToTemplate(w);
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
    clone,
    reset,
  };
});
