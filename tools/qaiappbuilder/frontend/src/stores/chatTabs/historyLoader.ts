// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * History-page fetching for the chat store (cohesion split, ARCH-1).
 *
 * The V2 messages endpoint pages FORWARD by absolute position; V1's UX is
 * newest-first with scroll-up "load older". The position arithmetic that
 * bridges the two, plus the two network round-trips, are pure with respect
 * to Pinia (they take an injected `apiJson` and return mapped data), so
 * they live here. The store actions (`loadHistoryMessages` /
 * `loadMoreMessages`) keep the reactive concerns: tab lookups, re-entrancy
 * guards, and `_patchTab`.
 *
 * V2 backend specifics (TestClient-verified):
 *   - `GET …/messages?cursor=position:<int>&limit=<n>` returns a FORWARD,
 *     ascending page starting at `position`.
 *   - `cursor=null` ⇒ oldest page (position 0).
 *   - `next_cursor="position:<lastPos+1>"` points at the next *newer* page
 *     (null when forward-exhausted).
 *   - `GET …/conversations/{id}` carries `message_count`.
 */
import type { ChatMessage } from "../_chatTabsTypes";
import type { apiJson } from "@/api";
import { mapHistoryItems, type HistoryMessagesPage } from "./historyMapper";

/** The injected `apiJson` (the store still lazy-imports it at runtime; we
 *  only borrow its type here so the call sites stay fully type-checked). */
type ApiJson = typeof apiJson;

/** Result of the newest-page fetch: the mapped messages plus the absolute
 *  start position so the store can seed `messagesOldestPos` / paging. */
export interface NewestPageResult {
  messages: ChatMessage[];
  startPos: number;
  /** Promote-ready detection carried on the conversation summary (backend
   *  migration 057). `null` when the backend never detected (legacy) or the
   *  summary lookup failed. Consumed by the store to seed `tab.detectedModel`
   *  so the promote CTA needs ZERO on-open disk scans. */
  detectedModel:
    | {
        workdir: string;
        variants: { precision: string; label: string }[];
        checkedAt?: string;
      }
    | null;
}

/** Result of an older-page fetch (scroll-up). `messages` is empty when the
 *  backend returned nothing (defensive end-of-history). */
export interface OlderPageResult {
  messages: ChatMessage[];
  newStart: number;
}

/**
 * Fetch the *newest* page for a conversation (V1 newest-first view).
 *
 * Resolves the total `message_count` to compute
 * `startPos = max(0, count - pageSize)` and fetches forward from there.
 * Resilient: a failed count lookup falls back to position 0 (oldest page).
 */
export async function fetchNewestPage(
  apiJson: ApiJson,
  convId: string,
  pageSize: number,
): Promise<NewestPageResult> {
  const encId = encodeURIComponent(convId);
  // 1) Resolve the total message count to find the newest page's start
  //    position (V1 reads data.total from the page; V2 exposes it on the
  //    conversation summary).
  //
  // Resilience (P1-3/P1-4): a SINGLE failed count lookup previously fell back
  // to position 0 — showing the OLDEST page instead of the newest, AND setting
  // startPos=0 so the store concluded `hasMoreMessages=false` and hid the
  // scroll-up "load older" entry. We retry the count once before falling back,
  // since it's a transient-failure-prone extra round-trip. The fallback page is
  // still position 0, but only after the retry also failed (rare).
  let total = 0;
  // Promote-ready detection carried on the same summary (migration 057). Read
  // it from the count-lookup response so opening a conversation needs no extra
  // round-trip and ZERO on-open disk scans.
  let detectedModel: NewestPageResult["detectedModel"] = null;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const summary = await apiJson<{
        message_count?: number;
        detected_model?: {
          workdir?: unknown;
          variants?: unknown;
          checked_at?: unknown;
        } | null;
      }>("GET", `/api/chat/conversations/${encId}`);
      total =
        typeof summary.message_count === "number" && summary.message_count > 0
          ? summary.message_count
          : 0;
      detectedModel = _normaliseDetectedModel(summary.detected_model);
      break;
    } catch {
      total = 0;
      if (attempt === 0) {
        await new Promise((r) => setTimeout(r, 150));
      }
    }
  }
  const startPos = Math.max(0, total - pageSize);
  const query =
    total > 0
      ? `?cursor=position:${startPos}&limit=${pageSize}`
      : `?limit=${pageSize}`;
  const res = await apiJson<HistoryMessagesPage>(
    "GET",
    `/api/chat/conversations/${encId}/messages${query}`,
  );
  return { messages: mapHistoryItems(res.items, convId), startPos, detectedModel };
}

/**
 * Normalise a raw backend ``detected_model`` object into the frontend camelCase
 * shape, tolerating a missing / malformed value (returns ``null``). Keeps the
 * store + composable free of wire-shape validation.
 */
function _normaliseDetectedModel(
  raw:
    | { workdir?: unknown; variants?: unknown; checked_at?: unknown }
    | null
    | undefined,
): NewestPageResult["detectedModel"] {
  if (raw === null || raw === undefined || typeof raw !== "object") return null;
  const workdir = typeof raw.workdir === "string" ? raw.workdir : "";
  const variants: { precision: string; label: string }[] = [];
  if (Array.isArray(raw.variants)) {
    for (const v of raw.variants) {
      if (v === null || typeof v !== "object") continue;
      const precision = (v as Record<string, unknown>).precision;
      const label = (v as Record<string, unknown>).label;
      if (typeof precision === "string" && typeof label === "string") {
        variants.push({ precision, label });
      }
    }
  }
  const checkedAt =
    typeof raw.checked_at === "string" ? raw.checked_at : undefined;
  return { workdir, variants, ...(checkedAt !== undefined ? { checkedAt } : {}) };
}

/**
 * Fetch one older page given the current oldest position (V1 scroll-up
 * "load older"). Pages *backwards*:
 *   newStart = max(0, oldestPos - pageSize)
 *   limit    = oldestPos - newStart   (exact older slice)
 */
export async function fetchOlderPage(
  apiJson: ApiJson,
  convId: string,
  oldestPos: number,
  pageSize: number,
): Promise<OlderPageResult> {
  const encId = encodeURIComponent(convId);
  const newStart = Math.max(0, oldestPos - pageSize);
  const limit = oldestPos - newStart;
  const res = await apiJson<HistoryMessagesPage>(
    "GET",
    `/api/chat/conversations/${encId}/messages?cursor=position:${newStart}&limit=${limit}`,
  );
  return { messages: mapHistoryItems(res.items, convId), newStart };
}
