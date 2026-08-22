// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useCcAgents — V1-parity Claude Code sub-agent editor state + handlers.
 *
 * Extracted from `ClaudeCodeConfigPanel.vue` (cohesion split). Owns the
 * three-ref editor state (`editingAgent` / `editingAgentName` /
 * `agentFormError`), the five action handlers (`openNewAgent` /
 * `openEditAgent` / `saveAgent` / `deleteAgent` / `cancelAgent`), and
 * the `agentNames` computed.
 *
 * Saves are local-only — `saveAgent` / `deleteAgent` mutate
 * `cfg.agents` (replace-style assignment, the same pattern `useCcAuth`
 * uses for `cfg.auth_env`); the host's `saveConfig` then PUTs the full
 * `cfg` to the backend, exactly as before. No API calls, no toasts,
 * no watch, no lifecycle hooks — purely a small reactive sub-state
 * machine plus its action surface.
 */
import { computed, ref, type ComputedRef, type Ref } from "vue";
import { useI18n } from "vue-i18n";

/** V1 parity: Claude Code sub-agent shape. */
export interface CcAgent {
  description?: string;
  prompt?: string;
  tools?: string[];
  model?: string;
  maxTurns?: number;
  permissionMode?: string;
}

/** Minimal slice of the host's `CcConfig` we read/write. */
export interface CcAgentsConfigShape {
  agents: Record<string, CcAgent>;
}

export interface UseCcAgentsReturn {
  editingAgent: Ref<CcAgent | null>;
  editingAgentName: Ref<string>;
  agentFormError: Ref<string>;
  agentNames: ComputedRef<string[]>;
  openNewAgent: () => void;
  openEditAgent: (name: string) => void;
  saveAgent: () => void;
  deleteAgent: (name: string) => void;
  cancelAgent: () => void;
}

export function useCcAgents<T extends CcAgentsConfigShape>(opts: {
  /** Reactive config object — composable reads/writes `cfg.agents`. */
  cfg: T;
}): UseCcAgentsReturn {
  const { t } = useI18n();
  const { cfg } = opts;

  const editingAgent = ref<CcAgent | null>(null);
  const editingAgentName = ref("");
  const agentFormError = ref("");

  function openNewAgent(): void {
    editingAgentName.value = "";
    editingAgent.value = {
      description: "",
      prompt: "",
      model: "",
      maxTurns: undefined,
    };
    agentFormError.value = "";
  }

  function openEditAgent(name: string): void {
    const a = cfg.agents[name] ?? {};
    editingAgentName.value = name;
    editingAgent.value = {
      description: a.description ?? "",
      prompt: a.prompt ?? "",
      model: a.model ?? "",
      maxTurns: a.maxTurns,
    };
    agentFormError.value = "";
  }

  function saveAgent(): void {
    if (!editingAgent.value) return;
    const name = editingAgentName.value.trim();
    if (!name) {
      agentFormError.value = t(
        "aiCoding.config.agentNameRequired",
        "Name is required",
      );
      return;
    }
    if (!/^[a-zA-Z0-9_-]+$/.test(name)) {
      agentFormError.value = t(
        "aiCoding.config.agentNameInvalid",
        "Name may only contain letters, digits, _ and -",
      );
      return;
    }
    if (!editingAgent.value.description?.trim()) {
      agentFormError.value = t(
        "aiCoding.config.agentDescRequired",
        "Description is required",
      );
      return;
    }
    if (!editingAgent.value.prompt?.trim()) {
      agentFormError.value = t(
        "aiCoding.config.agentPromptRequired",
        "Prompt is required",
      );
      return;
    }
    const entry: CcAgent = {
      description: editingAgent.value.description.trim(),
      prompt: editingAgent.value.prompt.trim(),
    };
    if (editingAgent.value.model?.trim()) {
      entry.model = editingAgent.value.model.trim();
    }
    if (editingAgent.value.maxTurns) {
      entry.maxTurns = Number(editingAgent.value.maxTurns);
    }
    cfg.agents = { ...cfg.agents, [name]: entry };
    editingAgent.value = null;
    editingAgentName.value = "";
  }

  function deleteAgent(name: string): void {
    const next = { ...cfg.agents };
    delete next[name];
    cfg.agents = next;
  }

  function cancelAgent(): void {
    editingAgent.value = null;
    editingAgentName.value = "";
    agentFormError.value = "";
  }

  const agentNames = computed(() => Object.keys(cfg.agents));

  return {
    editingAgent,
    editingAgentName,
    agentFormError,
    agentNames,
    openNewAgent,
    openEditAgent,
    saveAgent,
    deleteAgent,
    cancelAgent,
  };
}
