// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * usePromptHistory — recent + favorite prompt store for the composer.
 *
 * Backs the chat composer's "history" button (the clock icon in `.rit-right`).
 * It records every prompt the user sends and lets them pin favorites for quick
 * reuse, both persisted to localStorage so they survive reloads/restarts.
 *
 * Two independent lists (deliberately separate — different lifecycles):
 *   - `recent`    — auto-recorded on each send, deduped (same text → moved to
 *                   front, never duplicated), ring-truncated to MAX_RECENT.
 *   - `favorites` — user-pinned via ⭐; protected (never evicted by recency).
 *                   The popover shows up to FAV_DEFAULT_VISIBLE by default.
 *
 * Persistence mirrors `stores/chatTabs/chatTabsPersistence.ts`' defensive
 * `safeStorage()` pattern (private-mode / SSR / quota safe — every op degrades
 * to a no-op). State is module-level so all composer instances share ONE list
 * (mirrors how the multi-tab store is a singleton); the refs are created once.
 *
 * Frontend-only by design: `/api/preferences` is a strict 3-field allow-list
 * (`selected_model_id` / `selected_model_provider` / `selected_service_model`)
 * and would drop a prompt-history field via `extra="ignore"`, so localStorage
 * is the correct home for this local-only convenience feature.
 */
import { ref, type Ref } from "vue";

/** One stored prompt entry. */
export interface PromptEntry {
  /** Stable id (used as v-for key and for single-entry removal). */
  readonly id: string;
  /** The prompt text (trimmed, plain — never includes an image prefix). */
  readonly text: string;
  /** Last used / favorited epoch-ms (drives ordering, newest first). */
  readonly ts: number;
}

const RECENT_STORAGE_KEY = "qai.chat.promptHistory.v1";
const FAVORITES_STORAGE_KEY = "qai.chat.promptFavorites.v1";

/** Ring-truncation cap for the recent list (keeps localStorage tiny). */
export const MAX_RECENT = 50;
/** How many favorites the popover shows before "show all" (user spec: 5). */
export const FAV_DEFAULT_VISIBLE = 5;
/** How many recent entries the popover shows (the full stored history; the
 * popover body scrolls when the list is tall). */
export const RECENT_VISIBLE = MAX_RECENT;

function safeStorage(): Storage | null {
  if (typeof globalThis === "undefined") {
    return null;
  }
  const candidate = (globalThis as { localStorage?: Storage }).localStorage;
  if (candidate === undefined) {
    return null;
  }
  try {
    const probe = "__qai_prompt_history_probe__";
    candidate.setItem(probe, "1");
    candidate.removeItem(probe);
    return candidate;
  } catch {
    return null;
  }
}

/** Parse + sanitise a persisted entry array; drops anything malformed. */
function loadList(key: string): PromptEntry[] {
  const storage = safeStorage();
  if (storage === null) {
    return [];
  }
  const raw = storage.getItem(key);
  if (raw === null) {
    return [];
  }
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) {
      return [];
    }
    const out: PromptEntry[] = [];
    for (const item of parsed) {
      if (item === null || typeof item !== "object") {
        continue;
      }
      const rec = item as Record<string, unknown>;
      const text = typeof rec.text === "string" ? rec.text : "";
      if (text.trim() === "") {
        continue;
      }
      const id = typeof rec.id === "string" ? rec.id : makeId();
      const ts = typeof rec.ts === "number" ? rec.ts : Date.now();
      out.push({ id, text, ts });
    }
    return out;
  } catch {
    return [];
  }
}

/** Persist a list (no-op when storage is unavailable / quota exceeded). */
function saveList(key: string, list: readonly PromptEntry[]): void {
  const storage = safeStorage();
  if (storage === null) {
    return;
  }
  try {
    storage.setItem(key, JSON.stringify(list));
  } catch {
    // Quota / serialisation failure — best-effort, ignore.
  }
}

let idCounter = 0;
function makeId(): string {
  idCounter += 1;
  return `p${Date.now().toString(36)}-${idCounter.toString(36)}`;
}

// ── Module-level singleton state (shared across all composer instances) ──────
const recent: Ref<PromptEntry[]> = ref(loadList(RECENT_STORAGE_KEY));
const favorites: Ref<PromptEntry[]> = ref(loadList(FAVORITES_STORAGE_KEY));

function normalize(text: string): string {
  return text.trim();
}

/**
 * Comparison key for de-duplication. Case-insensitive and whitespace-collapsed
 * so prompts that differ only in casing or spacing are treated as the same
 * entry (the re-entered one is moved to the front). The stored/displayed text
 * still uses the original (normalize-trimmed) form.
 */
function dedupeKey(text: string): string {
  return normalize(text).toLowerCase().replace(/\s+/g, " ");
}

/**
 * Record a freshly-sent prompt into the recent list: dedupe by comparison key
 * (an existing match is moved to the front with the freshly-entered text and a
 * refreshed timestamp rather than duplicated), then ring-truncate to
 * MAX_RECENT. Blank input is ignored.
 */
function recordSent(text: string): void {
  const trimmed = normalize(text);
  if (trimmed === "") {
    return;
  }
  const key = dedupeKey(trimmed);
  const next = recent.value.filter((e) => dedupeKey(e.text) !== key);
  next.unshift({ id: makeId(), text: trimmed, ts: Date.now() });
  recent.value = next.slice(0, MAX_RECENT);
  saveList(RECENT_STORAGE_KEY, recent.value);
}

/** True when `text` is currently pinned as a favorite (exact match). */
function isFavorite(text: string): boolean {
  const trimmed = normalize(text);
  return favorites.value.some((e) => e.text === trimmed);
}

/**
 * Toggle a prompt's favorite state. Pinning prepends a fresh entry; unpinning
 * removes the exact-text match. Blank input is ignored.
 */
function toggleFavorite(text: string): void {
  const trimmed = normalize(text);
  if (trimmed === "") {
    return;
  }
  if (isFavorite(trimmed)) {
    favorites.value = favorites.value.filter((e) => e.text !== trimmed);
  } else {
    favorites.value = [
      { id: makeId(), text: trimmed, ts: Date.now() },
      ...favorites.value,
    ];
  }
  saveList(FAVORITES_STORAGE_KEY, favorites.value);
}

/** Remove a single recent entry by id. */
function removeRecent(id: string): void {
  recent.value = recent.value.filter((e) => e.id !== id);
  saveList(RECENT_STORAGE_KEY, recent.value);
}

/** Remove a single favorite entry by id. */
function removeFavorite(id: string): void {
  favorites.value = favorites.value.filter((e) => e.id !== id);
  saveList(FAVORITES_STORAGE_KEY, favorites.value);
}

/** Clear all recent history (favorites are untouched). */
function clearRecent(): void {
  recent.value = [];
  saveList(RECENT_STORAGE_KEY, recent.value);
}

export function usePromptHistory() {
  return {
    recent,
    favorites,
    recordSent,
    isFavorite,
    toggleFavorite,
    removeRecent,
    removeFavorite,
    clearRecent,
  };
}
