// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * chatTabsPersistence — persist the multi-tab *layout* across reloads/restarts.
 *
 * Only a lightweight **skeleton** is stored: which tabs are open, their
 * conversation binding + title + order, and which one is active. Runtime state
 * (messages, streaming buffers, AbortController / WebSocket handles, perf, etc.)
 * is NEVER persisted — it is rebuilt on load by reopening each tab and lazily
 * fetching its conversation history from the backend (the existing
 * `loadHistoryMessages` path). This keeps localStorage tiny and avoids
 * serialising non-JSON runtime objects (State-Truth-First: the backend remains
 * the source of truth for message content; localStorage only remembers
 * "which sessions I had open").
 *
 * Sub-agent tabs are persisted as their own top-level entries alongside
 * main-agent tabs (β flat tab-strip model): EVERY tab — main-agent or sub-agent (at any
 * depth) — is persisted as its own top-level entry keyed by
 * `kind: "chat"` (default) or `kind: "subagent"` + `subagentId`. There is no
 * nested `subAgentIds` tail array; sub-agent tabs restore via their own
 * `_hydrateRestoredSubAgentTab` path just like they were opened fresh.
 *
 * Defensive storage access mirrors `useChatPersistence.ts` (private-mode /
 * SSR / disabled-storage safe — every op degrades to a no-op).
 */

const STORAGE_KEY = "qai.chat.tabsLayout.v1";

/** One persisted tab entry (skeleton only). */
export interface PersistedTabEntry {
  /** Backend conversation id, or null for a brand-new never-sent tab.
   *  For a sub-agent tab (`kind === "subagent"`) this MUST be null — the
   *  authoritative key is `subagentId`; persisting the parent's conversation
   *  id here would let `loadHistoryMessages` load the PARENT transcript into
   *  the sub-agent tab on restore. */
  readonly conversationId: string | null;
  /** Display title (may be empty → UI shows the localized "untitled"). */
  readonly title: string;
  /**
   * Tab kind (V2 enhancement; appended). Defaults to `"chat"` when absent so
   * pre-feature persisted layouts round-trip byte-identically.
   *
   * `"subagent"` entries are persisted verbatim (β flat tab-strip model):
   * every sub-agent tab — at any depth — is a first-class top-level entry
   * keyed by `subagentId`, restored via `_hydrateRestoredSubAgentTab`.
   */
  readonly kind?: "chat" | "subagent";
  /**
   * Sub-agent id — REQUIRED on `kind === "subagent"` entries; ignored on
   * normal chat entries. The restore path uses this id to fetch the
   * sub-agent detail + transcript.
   */
  readonly subagentId?: string;
  /**
   * Per-session ("this conversation only") tool / SKILL override
   * (OVERRIDE/DIFF — see `ChatTab.sessionToolOverride`). Persisted so the
   * user's toggles survive closing+reopening the tab AND a full reload. Only
   * the names switched OFF are stored (an empty/absent override = follow
   * global defaults). Omitted entirely when there is no override, so a tab
   * without one serialises byte-identically to the pre-feature shape.
   */
  readonly sessionToolOverride?: {
    readonly disabledTools: readonly string[];
    readonly disabledSkills: readonly string[];
  };
}

export interface PersistedTabsLayout {
  readonly version: 1;
  readonly tabs: readonly PersistedTabEntry[];
  /** Index into `tabs` of the active tab; clamped on restore. */
  readonly activeIndex: number;
}

function safeStorage(): Storage | null {
  if (typeof globalThis === "undefined") {
    return null;
  }
  const candidate = (globalThis as { localStorage?: Storage }).localStorage;
  if (candidate === undefined) {
    return null;
  }
  try {
    const probe = "__qai_chat_tabs_layout_probe__";
    candidate.setItem(probe, "1");
    candidate.removeItem(probe);
    return candidate;
  } catch {
    return null;
  }
}

/** Defensively parse a persisted `sessionToolOverride` (drop malformed /
 *  empty values → `undefined`, i.e. "no override / follow global defaults"). */
function sanitizeOverride(
  raw: unknown,
): { disabledTools: string[]; disabledSkills: string[] } | undefined {
  if (raw === null || typeof raw !== "object") {
    return undefined;
  }
  const o = raw as {
    disabledTools?: unknown;
    disabledSkills?: unknown;
  };
  const toStrArray = (v: unknown): string[] =>
    Array.isArray(v) ? v.filter((x): x is string => typeof x === "string" && x !== "") : [];
  const disabledTools = toStrArray(o.disabledTools);
  const disabledSkills = toStrArray(o.disabledSkills);
  if (disabledTools.length === 0 && disabledSkills.length === 0) {
    return undefined;
  }
  return { disabledTools, disabledSkills };
}

/** Read the persisted layout, or `null` when absent / unreadable / malformed. */
export function loadTabsLayout(): PersistedTabsLayout | null {
  const storage = safeStorage();
  if (storage === null) {
    return null;
  }
  const raw = storage.getItem(STORAGE_KEY);
  if (raw === null) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw) as PersistedTabsLayout;
    if (parsed.version !== 1 || !Array.isArray(parsed.tabs)) {
      return null;
    }
    // Sanitise each entry — drop anything that isn't shaped right.
    const tabs: PersistedTabEntry[] = [];
    for (const entry of parsed.tabs) {
      if (entry === null || typeof entry !== "object") {
        continue;
      }
      // Sub-agent entry (`kind: "subagent"` + non-empty `subagentId`).
      // Flat tab-strip model (β): every sub-agent tab — at any depth — is a
      // first-class top-level entry restored via
      // `_hydrateRestoredSubAgentTab`. `conversationId` is forced to null
      // here as defence in depth: the authoritative key is `subagentId`, and
      // the sub-agent's root_conversation_id is re-fetched from the detail
      // GET on restore. A malformed entry (missing/empty subagentId) is
      // dropped.
      if (entry.kind === "subagent") {
        const subagentId =
          typeof entry.subagentId === "string" && entry.subagentId !== ""
            ? entry.subagentId
            : null;
        if (subagentId === null) {
          continue;
        }
        const title = typeof entry.title === "string" ? entry.title : "";
        tabs.push({
          conversationId: null,
          title,
          kind: "subagent",
          subagentId,
        });
        continue;
      }
      const conversationId =
        typeof entry.conversationId === "string" ? entry.conversationId : null;
      const title = typeof entry.title === "string" ? entry.title : "";
      const sessionToolOverride = sanitizeOverride(entry.sessionToolOverride);
      // Assemble — only emit optional fields when they carry real data, so
      // a vanilla main-agent tab serialises byte-identically to the pre-
      // feature shape.
      const built: PersistedTabEntry = {
        conversationId,
        title,
        ...(sessionToolOverride !== undefined ? { sessionToolOverride } : {}),
      };
      tabs.push(built);
    }
    if (tabs.length === 0) {
      return null;
    }
    const activeIndex =
      typeof parsed.activeIndex === "number" &&
      parsed.activeIndex >= 0 &&
      parsed.activeIndex < tabs.length
        ? parsed.activeIndex
        : 0;
    return { version: 1, tabs, activeIndex };
  } catch {
    return null;
  }
}

/** Persist the layout (no-op when storage is unavailable). */
export function saveTabsLayout(layout: PersistedTabsLayout): void {
  const storage = safeStorage();
  if (storage === null) {
    return;
  }
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(layout));
  } catch {
    // Quota / serialisation failure — best-effort, ignore.
  }
}

/** Remove the persisted layout. */
export function clearTabsLayout(): void {
  const storage = safeStorage();
  if (storage === null) {
    return;
  }
  try {
    storage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}

// ---------------------------------------------------------------------------
// Per-conversation session tool / SKILL override store (keyed by
// conversationId), DECOUPLED from the open-tabs layout above.
//
// Why a separate store: the layout above only records *currently-open* tabs,
// so closing a tab drops its entry (the watcher re-saves the reduced layout) →
// the override would be lost on close+reopen. Keying the override by
// conversationId in its own map makes it survive: closing a tab, reopening the
// same conversation (a fresh `openTab({conversationId})`), AND a full reload.
// State-Truth-First: this map is the single persisted truth for "this
// conversation's temporary tool toggles"; the open-tabs layout no longer needs
// to carry it (kept there too only for backward-compat reads).
// ---------------------------------------------------------------------------
const STORAGE_KEY_OVERRIDES = "qai.chat.sessionToolOverrides.v1";

export interface PersistedSessionToolOverride {
  readonly disabledTools: readonly string[];
  readonly disabledSkills: readonly string[];
  /** TOMBSTONE marker: an empty-but-present record meaning "the user
   *  explicitly chose all-on for this key" (vs "never set"). Only sub-agent
   *  keys use it — so a sub-agent that the user reset to all-on does NOT
   *  re-inherit the parent conversation's override on reopen. Absent on normal
   *  (non-empty) overrides. */
  readonly explicit?: boolean;
}

type OverrideMap = Record<string, PersistedSessionToolOverride>;

/** Sanitise a stored map entry, PRESERVING an explicit-empty tombstone
 *  (`{disabledTools:[],disabledSkills:[],explicit:true}`). Returns `undefined`
 *  for a non-explicit empty/malformed value (→ "no override"). */
function sanitizeStoredEntry(
  raw: unknown,
): PersistedSessionToolOverride | undefined {
  const ov = sanitizeOverride(raw);
  if (ov !== undefined) {
    return ov;
  }
  // Non-empty parse failed → check for an explicit-empty tombstone.
  if (
    raw !== null &&
    typeof raw === "object" &&
    (raw as { explicit?: unknown }).explicit === true
  ) {
    return { disabledTools: [], disabledSkills: [], explicit: true };
  }
  return undefined;
}

function loadOverrideMap(): OverrideMap {
  const storage = safeStorage();
  if (storage === null) {
    return {};
  }
  const raw = storage.getItem(STORAGE_KEY_OVERRIDES);
  if (raw === null) {
    return {};
  }
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (parsed === null || typeof parsed !== "object") {
      return {};
    }
    const out: OverrideMap = {};
    for (const [cid, val] of Object.entries(parsed as Record<string, unknown>)) {
      const ov = sanitizeStoredEntry(val);
      if (ov !== undefined) {
        out[cid] = ov;
      }
    }
    return out;
  } catch {
    return {};
  }
}

function saveOverrideMap(map: OverrideMap): void {
  const storage = safeStorage();
  if (storage === null) {
    return;
  }
  try {
    if (Object.keys(map).length === 0) {
      storage.removeItem(STORAGE_KEY_OVERRIDES);
    } else {
      storage.setItem(STORAGE_KEY_OVERRIDES, JSON.stringify(map));
    }
  } catch {
    // Quota / serialisation failure — best-effort, ignore.
  }
}

/** Read the persisted override for one conversation (or `undefined`). */
export function loadSessionToolOverride(
  conversationId: string,
): PersistedSessionToolOverride | undefined {
  if (!conversationId) {
    return undefined;
  }
  return loadOverrideMap()[conversationId];
}

/**
 * Upsert (or, with `null`/empty, delete) one conversation's override. Survives
 * tab close + reopen + reload (keyed by conversationId, not by open-tab layout).
 * A no-op when there is no conversationId (a brand-new unsent tab has nothing
 * to key on yet — its override rides only in memory until the conversation is
 * created, at which point a later toggle persists it).
 */
export function saveSessionToolOverride(
  conversationId: string,
  override: PersistedSessionToolOverride | null | undefined,
  opts: { explicitEmpty?: boolean } = {},
): void {
  if (!conversationId) {
    return;
  }
  const map = loadOverrideMap();
  const ov =
    override === null || override === undefined
      ? undefined
      : sanitizeOverride(override);
  if (ov === undefined) {
    // Empty / cleared. Normally we DELETE the entry (no override → follow the
    // default). But `explicitEmpty` writes a TOMBSTONE — an empty-but-present
    // record — to mean "the user explicitly chose all-on for THIS key". Used by
    // sub-agent tabs: once the user has touched the sub-agent's tools (even
    // resetting to all-on), that choice must STICK and NOT silently re-inherit
    // the parent conversation's override on reopen. A main-agent tab never
    // passes `explicitEmpty` (it has no parent to inherit from → delete is
    // correct, keeping the store small).
    if (opts.explicitEmpty) {
      map[conversationId] = {
        disabledTools: [],
        disabledSkills: [],
        explicit: true,
      };
    } else {
      if (!(conversationId in map)) {
        return;
      }
      delete map[conversationId];
    }
  } else {
    map[conversationId] = ov;
  }
  saveOverrideMap(map);
}
