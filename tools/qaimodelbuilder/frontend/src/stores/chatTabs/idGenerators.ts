// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Monotonic client-side id generators for the chat store (cohesion split,
 * ARCH-1). Moved verbatim from `chatTabs.ts`.
 *
 * Each generator pairs `Date.now()` (base-36) with a per-kind monotonic
 * counter so two ids minted in the same millisecond never collide (V1 used
 * a bare `Date.now()` for queue ids — the counter hardens that).
 */
import type { TabId } from "../_chatTabsTypes";

let _tabIdCounter = 0;
export function nextLocalTabId(): TabId {
  _tabIdCounter = _tabIdCounter + 1;
  return `tab-${Date.now().toString(36)}-${_tabIdCounter.toString(36)}`;
}

let _msgIdCounter = 0;
export function nextMessageId(): string {
  _msgIdCounter = _msgIdCounter + 1;
  return `msg-${Date.now().toString(36)}-${_msgIdCounter.toString(36)}`;
}

let _queueIdCounter = 0;
/** Stable client-side id for a queued message (V1 used `Date.now()`;
 *  a monotonic counter avoids collisions when two items enqueue in the
 *  same millisecond). */
export function nextQueueId(): string {
  _queueIdCounter = _queueIdCounter + 1;
  return `q-${Date.now().toString(36)}-${_queueIdCounter.toString(36)}`;
}
