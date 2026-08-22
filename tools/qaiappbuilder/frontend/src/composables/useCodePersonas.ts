// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useCodePersonas` — manage AI coding persona presets.
 *
 * Provides fetch / select / delete over coding personas.
 * Each persona bundles a system prompt prefix so the user can switch
 * "coding style" with one click.
 *
 * Endpoints:
 *   GET    /api/code-personas          → { selected, personas: [...] }
 *   POST   /api/code-personas/:id        body { prompt }  (save override)
 *   DELETE /api/code-personas/:id        → reset to built-in default
 */
import { ref, type Ref } from "vue";

import { apiJson } from "@/api";
import { useToastStore } from "@/stores/toast";

// ─── Types ───────────────────────────────────────────────────────────────────

export interface Persona {
  id: string;
  name: string;
  description: string;
  icon?: string;
  prompt?: string;
  /** Built-in default prompt (tail-appended by backend). */
  default_prompt?: string;
  /** True when the user has overridden this persona's prompt. */
  is_customized?: boolean;
  /** Tool permission groups (e.g. ["read", "edit", "command"]). */
  groups?: Array<string | [string, Record<string, string>]>;
  /** Built-in default groups (tail-appended by backend). */
  default_groups?: Array<string | [string, Record<string, string>]>;
  /** True when the user has overridden this persona's groups. */
  is_groups_customized?: boolean;
  system_prompt?: string;
  model_id?: string;
  enabled?: boolean;
}

export interface CreatePersonaPayload {
  name: string;
  description?: string;
  system_prompt: string;
  model_id?: string;
}

interface PersonaListResponse {
  selected: string | null;
  personas: Persona[];
}

// ─── Composable ──────────────────────────────────────────────────────────────

/**
 * Optional i18n hooks. The composable stays usable without them (English
 * fallback toasts) so unit tests can call `useCodePersonas()` outside a Vue
 * setup / i18n context. Callers inside a component setup (CodePersonasPanel)
 * pass `t` + `localizedName` so save / reset toasts are localized and carry
 * the persona's display name (V1 `app.js` parity: `t('codePersona.saveSuccess',
 * { name: localizedName(persona) })`).
 */
export interface CodePersonaI18n {
  t?: (key: string, named?: Record<string, unknown>) => string;
  localizedName?: (persona: Persona) => string;
  /** Reactive locale getter — called on each fetch to pick the current
   *  UI language (e.g. "en", "zh-CN", "zh-TW"). */
  getLocale?: () => string;
}

export function useCodePersonas(i18n: CodePersonaI18n = {}) {
  const personas: Ref<Persona[]> = ref([]);
  const activePersona: Ref<Persona | null> = ref(null);
  const loading: Ref<boolean> = ref(false);
  const saving: Ref<boolean> = ref(false);
  const deleteSupported: Ref<boolean> = ref(true);

  const toast = useToastStore();

  /** Translate `key` if a `t` was supplied, else return the English fallback. */
  function tr(key: string, fallback: string, named?: Record<string, unknown>): string {
    if (typeof i18n.t === "function") {
      const out = i18n.t(key, named);
      if (out && out !== key) return out;
    }
    return fallback;
  }

  /** Display name for toasts: caller's localizedName → persona.name → id. */
  function nameOf(persona: Persona | undefined): string {
    if (!persona) return "";
    if (typeof i18n.localizedName === "function") return i18n.localizedName(persona);
    return persona.name || persona.id;
  }

  async function fetchPersonas(): Promise<void> {
    loading.value = true;
    try {
      const currentLocale = typeof i18n.getLocale === "function" ? i18n.getLocale() : "";
      const localeParam = currentLocale ? `?locale=${encodeURIComponent(currentLocale)}` : "";
      const res = await apiJson<PersonaListResponse>("GET", `/api/code-personas${localeParam}`);
      personas.value = res.personas;
      // Set active persona from backend "selected" field
      if (res.selected) {
        activePersona.value = res.personas.find((p) => p.id === res.selected) ?? null;
      }
    } catch (e) {
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: `${tr("codePersona.loadFailed", "Failed to load coding personas")}: ${(e as Error).message}`,
        timeoutMs: 5000,
      });
    } finally {
      loading.value = false;
    }
  }

  function selectPersona(id: string | null): void {
    activePersona.value = id === null ? null : (personas.value.find((p) => p.id === id) ?? null);
  }

  /**
   * Persist the selected persona to the backend
   * (`POST /api/code-personas/select`) and update local state. Mirrors
   * V1's `useCodePersonas` persistence (the bare `selectPersona` above
   * is local-only for callers that don't want a round-trip).
   */
  async function selectPersonaPersisted(id: string): Promise<void> {
    // Optimistic local update first so the UI reacts immediately.
    selectPersona(id);
    try {
      await apiJson<PersonaListResponse>("POST", "/api/code-personas/select", {
        persona_id: id,
      });
    } catch (e) {
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: `${tr("codePersona.selectFailed", "Failed to select persona")}: ${(e as Error).message}`,
        timeoutMs: 5000,
      });
    }
  }

  async function createPersona(data: CreatePersonaPayload): Promise<Persona | null> {
    try {
      const created = await apiJson<Persona>("POST", "/api/code-personas", data);
      personas.value = [...personas.value, created];
      return created;
    } catch (e) {
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: `${tr("codePersona.createFailed", "Failed to create persona")}: ${(e as Error).message}`,
        timeoutMs: 5000,
      });
      return null;
    }
  }

  /**
   * Save a custom system prompt for a built-in persona via
   * `POST /api/code-personas/:id` (V1 `savePersonaPrompt` parity).
   * The local persona's `prompt` is updated on success.
   */
  async function savePersonaPrompt(id: string, prompt: string): Promise<boolean> {
    saving.value = true;
    try {
      await apiJson<{ status: string }>("POST", `/api/code-personas/${id}`, {
        prompt,
      });
      const idx = personas.value.findIndex((p) => p.id === id);
      if (idx >= 0) {
        const current = personas.value[idx] as Persona;
        const updated: Persona = { ...current, prompt };
        personas.value = [
          ...personas.value.slice(0, idx),
          updated,
          ...personas.value.slice(idx + 1),
        ];
        if (activePersona.value?.id === id) activePersona.value = updated;
      }
      toast.push({
        id: crypto.randomUUID(),
        kind: "success",
        message: tr("codePersona.saveSuccess", `"${nameOf(personas.value.find((p) => p.id === id))}" saved`, {
          name: nameOf(personas.value.find((p) => p.id === id)),
        }),
        timeoutMs: 3000,
      });
      return true;
    } catch (e) {
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: `${tr("codePersona.saveFailed", "Save failed")}: ${(e as Error).message}`,
        timeoutMs: 5000,
      });
      return false;
    } finally {
      saving.value = false;
    }
  }

  /**
   * Reset a built-in persona's prompt to its default via
   * `DELETE /api/code-personas/:id` (clears the user override), then
   * re-fetch so the restored default prompt is reflected locally
   * (V1 `resetPersona` parity). Unlike `deletePersona`, the persona
   * card is kept — built-in personas are never truly removed.
   */
  async function resetPersonaPrompt(id: string): Promise<boolean> {
    saving.value = true;
    // Capture the display name before the re-fetch (V1 toast carries it).
    const resetName = nameOf(personas.value.find((p) => p.id === id));
    try {
      await apiJson("DELETE", `/api/code-personas/${id}`);
      // Re-fetch to pull the restored built-in default prompt.
      await fetchPersonas();
      toast.push({
        id: crypto.randomUUID(),
        kind: "success",
        message: tr("codePersona.resetSuccess", `"${resetName}" reset to default`, {
          name: resetName,
        }),
        timeoutMs: 3000,
      });
      return true;
    } catch (e) {
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: `${tr("codePersona.resetFailed", "Reset failed")}: ${(e as Error).message}`,
        timeoutMs: 5000,
      });
      return false;
    } finally {
      saving.value = false;
    }
  }

  /**
   * Save custom tool permission groups for a built-in persona via
   * `POST /api/code-personas/:id` with `{ groups }`.
   * The local persona's `groups` is updated on success.
   */
  async function savePersonaGroups(
    id: string,
    groups: Array<string | [string, Record<string, string>]>,
  ): Promise<boolean> {
    saving.value = true;
    try {
      await apiJson<{ status: string }>("POST", `/api/code-personas/${id}`, {
        groups,
      });
      const idx = personas.value.findIndex((p) => p.id === id);
      if (idx >= 0) {
        const current = personas.value[idx] as Persona;
        const updated: Persona = { ...current, groups, is_groups_customized: true };
        personas.value = [
          ...personas.value.slice(0, idx),
          updated,
          ...personas.value.slice(idx + 1),
        ];
        if (activePersona.value?.id === id) activePersona.value = updated;
      }
      toast.push({
        id: crypto.randomUUID(),
        kind: "success",
        message: tr("codePersona.saveSuccess", `"${nameOf(personas.value.find((p) => p.id === id))}" saved`, {
          name: nameOf(personas.value.find((p) => p.id === id)),
        }),
        timeoutMs: 3000,
      });
      return true;
    } catch (e) {
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: `${tr("codePersona.saveFailed", "Save failed")}: ${(e as Error).message}`,
        timeoutMs: 5000,
      });
      return false;
    } finally {
      saving.value = false;
    }
  }

  async function deletePersona(id: string): Promise<void> {
    try {
      await apiJson("DELETE", `/api/code-personas/${id}`);
      personas.value = personas.value.filter((p) => p.id !== id);
      if (activePersona.value?.id === id) {
        activePersona.value = null;
      }
      toast.push({
        id: crypto.randomUUID(),
        kind: "success",
        message: tr("codePersona.removed", "Persona removed"),
        timeoutMs: 3000,
      });
    } catch (e) {
      // If DELETE returns 404/405, the endpoint doesn't support delete
      const msg = (e as Error).message;
      if (msg.includes("404") || msg.includes("405") || msg.includes("Not Found") || msg.includes("Method Not Allowed")) {
        deleteSupported.value = false;
        toast.push({
          id: crypto.randomUUID(),
          kind: "warning",
          message: tr("codePersona.deleteNotSupported", "Delete not supported by server"),
          timeoutMs: 5000,
        });
      } else {
        toast.push({
          id: crypto.randomUUID(),
          kind: "error",
          message: `${tr("codePersona.deleteFailed", "Failed to delete persona")}: ${msg}`,
          timeoutMs: 5000,
        });
      }
    }
  }

  return {
    personas,
    activePersona,
    loading,
    saving,
    deleteSupported,
    fetchPersonas,
    selectPersona,
    selectPersonaPersisted,
    createPersona,
    savePersonaPrompt,
    savePersonaGroups,
    resetPersonaPrompt,
    deletePersona,
  };
}
