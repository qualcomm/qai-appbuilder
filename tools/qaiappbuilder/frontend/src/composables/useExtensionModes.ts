// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Extension modes in the composer (design doc §3.1-§3.3, route B).
 *
 * - `entries`: the Extension menu (`GET /api/extensions/ui`), refreshed when
 *   the menu opens.
 * - The selected extension is per chat tab (a mode is UI state of the
 *   conversation view, design doc §3.2) and does NOT switch or clear the
 *   conversation. It is kept in memory only: reloading the page leaves the
 *   mode, like closing it.
 * - Built-in tool modes (App Builder, Model Builder, Coding, ...) and an
 *   extension mode are mutually exclusive per tab: entering a built-in mode
 *   leaves the extension mode (`clearExtensionMode`, called by the tabs
 *   store), and selecting an extension leaves the built-in mode (the menu
 *   only shows when no built-in mode is active; `activeExtensionsFor` sends
 *   nothing while one is).
 * - `activeExtensionsFor(tabId)` is what the transport sends with each turn
 *   (`active_extensions`), so the backend adds that extension's "active"
 *   prompt fragments to the turn's context.
 *
 * Module-level state (singleton), like the other composer composables.
 */
import { computed, reactive, ref } from "vue";

import { apiJson } from "@/api/http";
import { syncExtensionFrontends } from "@/composables/useExtensionFrontend";

export interface ExtensionUiField {
  key: string;
  type: "text" | "secret" | "bool" | "number" | "select";
  label: string;
  description: string;
  required: boolean;
  options?: string[];
  min?: number | null;
  max?: number | null;
  default?: unknown;
}

export interface ExtensionUiAction {
  id: string;
  label: string;
  /** One text per UI locale, when the manifest gave them (else absent / empty). */
  labels?: Record<string, string>;
  description: string;
  fields: ExtensionUiField[];
}

export interface ExtensionUiEntry {
  name: string;
  label: string;
  /** One text per UI locale, when the manifest gave them (else absent / empty). */
  labels?: Record<string, string>;
  icon: string;
  description: string;
  actions: ExtensionUiAction[];
  /** URL of the extension's front-end ES module (route A), `null` when it has none. */
  module?: string | null;
}

export interface ActionRunResult {
  ok: boolean;
  message: string;
  prompt: string;
}

const entries = ref<ExtensionUiEntry[]>([]);
const loaded = ref(false);
/** tab id -> selected extension name. */
const selected = reactive(new Map<string, string>());

async function refreshEntries(): Promise<void> {
  try {
    const r = await apiJson<{ extensions: ExtensionUiEntry[] }>("GET", "/api/extensions/ui");
    entries.value = r.extensions;
    // A selected extension that went away (uninstalled / disabled) ends its mode.
    const names = new Set(r.extensions.map((e) => e.name));
    for (const [tab, name] of [...selected.entries()]) {
      if (!names.has(name)) selected.delete(tab);
    }
    // Route A: (re)load the front-end modules that changed, drop the ones that went away.
    await syncExtensionFrontends(r.extensions);
  } catch {
    entries.value = [];
    await syncExtensionFrontends([]);
  } finally {
    loaded.value = true;
  }
}

function select(tabId: string, name: string): void {
  selected.set(tabId, name);
}

function clear(tabId: string): void {
  selected.delete(tabId);
}

/** Leave the tab's extension mode (a built-in mode was entered: the two are
 *  mutually exclusive). Called by the chat tabs store. */
export function clearExtensionMode(tabId: string): void {
  selected.delete(tabId);
}

function selectedFor(tabId: string | null | undefined): ExtensionUiEntry | null {
  if (!tabId) return null;
  const name = selected.get(tabId);
  return name ? (entries.value.find((e) => e.name === name) ?? null) : null;
}

/** Names to send as `active_extensions` for a turn in `tabId` ([] = none). */
export function activeExtensionsFor(tabId: string | null | undefined): string[] {
  if (!tabId) return [];
  const name = selected.get(tabId);
  return name ? [name] : [];
}

async function runAction(
  extension: string,
  actionId: string,
  values: Record<string, unknown>,
  ctx: { conversationId?: string | null; tabId?: string | null } = {},
): Promise<ActionRunResult> {
  return apiJson<ActionRunResult>(
    "POST",
    `/api/extensions/${encodeURIComponent(extension)}/actions/${encodeURIComponent(actionId)}`,
    { values, conversation_id: ctx.conversationId ?? "", tab_id: ctx.tabId ?? "" },
  );
}

export function useExtensionModes() {
  return {
    entries: computed(() => entries.value),
    loaded: computed(() => loaded.value),
    refreshEntries,
    select,
    clear,
    selectedFor,
    runAction,
  };
}

/** Test helper. */
export function __resetExtensionModesForTests(): void {
  entries.value = [];
  loaded.value = false;
  selected.clear();
}
