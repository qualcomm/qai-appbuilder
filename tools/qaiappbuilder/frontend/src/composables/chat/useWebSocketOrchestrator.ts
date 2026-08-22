// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useWebSocketOrchestrator — CROSS-CHANNEL WebSocket lifecycle coordination
 * (N.44).
 *
 * Scope — deliberately narrow (this is the boundary the design's risk section
 * draws itself): the orchestrator coordinates the things that are identical
 * for EVERY chat WebSocket and were previously duplicated per channel:
 *
 *   1. **Registry** — a channel hands over an `onSuspend` / `onResume` pair;
 *      it keeps its own socket, its own frame parsing and its own `from_seq`
 *      stitching.
 *   2. **Visibility coupling** — the page hidden CONTINUOUSLY for
 *      {@link HIDDEN_SUSPEND_DELAY_MS} (60s) suspends every channel; becoming
 *      visible again resumes them. A short hide (tab switch, alt-tab,
 *      screenshot) must NOT disturb a live stream, hence the 60s debounce.
 *   3. **Network coupling** — `offline` suspends every channel, `online`
 *      resumes them.
 *   4. **The backoff ladder** — {@link nextBackoffMs} is the ONE
 *      implementation of "how long before the next reconnect attempt" in the
 *      frontend. Every channel that retries a socket calls it with its own
 *      policy instead of open-coding `base * 2 ** attempt`.
 *   5. **De-duplication** — registering the same `id` twice bumps a subscriber
 *      count and keeps the FIRST hooks; the channel is released only when the
 *      count returns to 0.
 *
 * Explicit NON-goals (each channel keeps them, unchanged and tested):
 *   * never constructs a `WebSocket`;
 *   * never parses a frame / envelope;
 *   * never computes or applies a `from_seq` cursor;
 *   * never decides WHAT a suspend means for a given channel — a channel that
 *     only wants to pause its retry loop does exactly that, one that wants to
 *     drop and re-attach does that. The orchestrator only says WHEN.
 *
 * Suspend reasons are ref-counted as a SET, not a boolean: going offline while
 * hidden and then becoming visible must NOT resume (the link is still down).
 * `onResume` fires exactly when the last reason clears.
 */

// ---------------------------------------------------------------------------
// Backoff ladder — single truth source
// ---------------------------------------------------------------------------

/**
 * Per-channel backoff policy. The defaults ARE the sub-agent / stream
 * reconnect ladder (1s → 2s → 4s → 8s → 30s ceiling, ±20% jitter) that
 * `stores/chatTabs/transientHandles` used to own; it now delegates here, so
 * the ladder exists once.
 *
 * A channel with different requirements (the primary data-plane client retries
 * a HANDSHAKE inside a live turn, where a 1s first rung would be far too slow
 * and jitter would only make failures non-reproducible) overrides the fields
 * it needs rather than open-coding a second formula.
 */
export interface BackoffPolicy {
  /** Delay for attempt 0, doubled on each following rung. Default 1000. */
  readonly baseMs?: number;
  /**
   * How many doubling rungs exist before `ceilingMs` takes over. Default 4 —
   * i.e. attempts 0..3 are 1s/2s/4s/8s and attempt 4+ is the ceiling. This is
   * why the ladder jumps 8s → 30s instead of doubling to 16s.
   *
   * `Infinity` means "keep doubling, never reach a ceiling" — the shape the
   * bounded-budget channels want (they cap by attempt COUNT, not by delay, so
   * a ceiling would never be reached anyway and stating one is noise).
   */
  readonly rungs?: number;
  /** Delay for every attempt at or beyond `rungs`. Default 30_000. Ignored
   *  when `rungs` is `Infinity`. */
  readonly ceilingMs?: number;
  /**
   * Symmetric jitter as a fraction of the rung. Default 0.2 (±20%) so N tabs
   * dropped by the SAME server restart do not reconnect in lockstep and
   * re-thunder the server. `0` disables jitter (deterministic delays).
   */
  readonly jitterRatio?: number;
}

const DEFAULT_BASE_MS = 1_000;
const DEFAULT_RUNGS = 4;
const DEFAULT_CEILING_MS = 30_000;
const DEFAULT_JITTER_RATIO = 0.2;

/**
 * Delay in ms before reconnect attempt `attempt` (0-based).
 *
 * With the defaults: 1s → 2s → 4s → 8s → 30s → 30s …, each jittered by ±20%.
 * Never returns less than 1 (a 0ms retry would busy-loop).
 */
export function nextBackoffMs(
  attempt: number,
  policy?: BackoffPolicy,
): number {
  const baseMs = policy?.baseMs ?? DEFAULT_BASE_MS;
  const rungs = policy?.rungs ?? DEFAULT_RUNGS;
  const ceilingMs = policy?.ceilingMs ?? DEFAULT_CEILING_MS;
  const jitterRatio = policy?.jitterRatio ?? DEFAULT_JITTER_RATIO;
  const step = attempt > 0 ? Math.floor(attempt) : 0;
  const rung = step < rungs ? baseMs * Math.pow(2, step) : ceilingMs;
  if (jitterRatio <= 0) {
    return Math.max(1, Math.round(rung));
  }
  const spread = rung * jitterRatio;
  return Math.max(
    1,
    Math.round(rung - spread + Math.random() * 2 * spread),
  );
}

// ---------------------------------------------------------------------------
// Channel registry
// ---------------------------------------------------------------------------

/**
 * What a channel does when the orchestrator tells it the page went away (and
 * came back). Both are invoked at most once per transition and must be
 * idempotent — a channel with nothing to pause implements a no-op.
 */
export interface WebSocketChannelHooks {
  /**
   * The page has been hidden for 60s, or the browser reports offline. The
   * channel should stop burning reconnect attempts (and may drop a socket that
   * cannot deliver anything useful right now).
   */
  onSuspend(): void;
  /**
   * The page is visible again and online. The channel should retry NOW rather
   * than waiting out a backoff rung that was armed before the outage.
   */
  onResume(): void;
}

export interface WebSocketChannelRegistration {
  /** Release this registration. Idempotent; the channel is only really
   *  released when the last subscriber for its id unregisters. */
  unregister(): void;
}

interface ChannelRecord {
  readonly hooks: WebSocketChannelHooks;
  subscribers: number;
}

/** How long the page must stay CONTINUOUSLY hidden before channels suspend. */
export const HIDDEN_SUSPEND_DELAY_MS = 60_000;

type SuspendReason = "hidden" | "offline";

const channels = new Map<string, ChannelRecord>();
/** Active suspend reasons. Non-empty ⇔ every channel is suspended. */
const suspendReasons = new Set<SuspendReason>();
let hiddenTimer: ReturnType<typeof setTimeout> | null = null;
let listenersInstalled = false;

function clearHiddenTimer(): void {
  if (hiddenTimer !== null) {
    clearTimeout(hiddenTimer);
    hiddenTimer = null;
  }
}

function fanOutSuspend(): void {
  for (const record of [...channels.values()]) {
    try {
      record.hooks.onSuspend();
    } catch {
      // A misbehaving channel must not stop the others from suspending.
    }
  }
}

function fanOutResume(): void {
  for (const record of [...channels.values()]) {
    try {
      record.hooks.onResume();
    } catch {
      // Same containment as suspend.
    }
  }
}

function addSuspendReason(reason: SuspendReason): void {
  const wasSuspended = suspendReasons.size > 0;
  suspendReasons.add(reason);
  if (!wasSuspended) {
    fanOutSuspend();
  }
}

function dropSuspendReason(reason: SuspendReason): void {
  if (!suspendReasons.delete(reason)) {
    return;
  }
  if (suspendReasons.size === 0) {
    fanOutResume();
  }
}

function onVisibilityChange(): void {
  if (typeof document === "undefined") {
    return;
  }
  if (document.visibilityState === "hidden") {
    // Debounce: only a hide that LASTS suspends. Re-arming here is safe —
    // `clearHiddenTimer` on every transition guarantees at most one timer.
    clearHiddenTimer();
    hiddenTimer = setTimeout(() => {
      hiddenTimer = null;
      addSuspendReason("hidden");
    }, HIDDEN_SUSPEND_DELAY_MS);
    return;
  }
  clearHiddenTimer();
  dropSuspendReason("hidden");
}

/** `offline` / `online` are separate one-line listeners rather than one
 *  parameterised handler because `removeEventListener` needs the SAME function
 *  identity it was added with. */
function onOffline(): void {
  addSuspendReason("offline");
}

function onOnline(): void {
  dropSuspendReason("offline");
}

function installListeners(): void {
  if (listenersInstalled) {
    return;
  }
  listenersInstalled = true;
  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", onVisibilityChange);
    // Adopt the CURRENT visibility rather than waiting for the next
    // transition: a channel registered while the page is already hidden (a
    // background tab reconnecting) must start its own 60s window now, not stay
    // unsuspended until the user comes back and the state is moot.
    if (document.visibilityState === "hidden") {
      hiddenTimer = setTimeout(() => {
        hiddenTimer = null;
        addSuspendReason("hidden");
      }, HIDDEN_SUSPEND_DELAY_MS);
    }
  }
  if (typeof window !== "undefined") {
    window.addEventListener("offline", onOffline);
    window.addEventListener("online", onOnline);
  }
  // Same reason, for the link: `navigator.onLine` is the state, the
  // `offline` event is only the edge. A channel that registers during an
  // outage would otherwise never learn about it (the edge fired before any
  // listener existed) and would burn its whole retry budget against a dead
  // link.
  if (typeof navigator !== "undefined" && navigator.onLine === false) {
    addSuspendReason("offline");
  }
}

function removeListeners(): void {
  if (!listenersInstalled) {
    return;
  }
  listenersInstalled = false;
  if (typeof document !== "undefined") {
    document.removeEventListener("visibilitychange", onVisibilityChange);
  }
  if (typeof window !== "undefined") {
    window.removeEventListener("offline", onOffline);
    window.removeEventListener("online", onOnline);
  }
  clearHiddenTimer();
  // Drop the accumulated reasons with the listeners. Keeping them would strand
  // the orchestrator "suspended" forever: with no listeners the `online` /
  // `visible` edge that clears a reason can never be observed, so the next
  // channel to register would be suspended on stale news. `installListeners`
  // re-derives both reasons from the live environment instead.
  suspendReasons.clear();
}

/**
 * Register a channel's suspend/resume hooks under `id`.
 *
 * De-duplication: a second `register` with the SAME id keeps the first hooks
 * and bumps the subscriber count, so two call sites that legitimately share the
 * same logical channel (a store action re-entered by a reconnect, say) cannot
 * install two sets of callbacks. The channel is released — and, when it was
 * the last one, the DOM listeners are removed — once every subscriber has
 * unregistered.
 *
 * When the orchestrator is ALREADY suspended (registered during an offline
 * window / a long hide), the fresh channel is suspended immediately so it does
 * not sit retrying against a link the orchestrator knows is down.
 */
export function registerWebSocketChannel(
  id: string,
  hooks: WebSocketChannelHooks,
): WebSocketChannelRegistration {
  installListeners();
  const existing = channels.get(id);
  if (existing !== undefined) {
    existing.subscribers = existing.subscribers + 1;
  } else {
    channels.set(id, { hooks, subscribers: 1 });
    if (suspendReasons.size > 0) {
      try {
        hooks.onSuspend();
      } catch {
        // ignore — same containment as the fan-out helpers
      }
    }
  }
  let released = false;
  return {
    unregister(): void {
      if (released) {
        return;
      }
      released = true;
      const record = channels.get(id);
      if (record === undefined) {
        return;
      }
      record.subscribers = record.subscribers - 1;
      if (record.subscribers <= 0) {
        channels.delete(id);
      }
      if (channels.size === 0) {
        removeListeners();
      }
    },
  };
}

/** Subscriber count for `id` (0 when not registered). Introspection for tests
 *  and for callers that must not double-register. */
export function webSocketChannelSubscriberCount(id: string): number {
  return channels.get(id)?.subscribers ?? 0;
}

/** Ids of every currently registered channel. Introspection only. */
export function registeredWebSocketChannelIds(): readonly string[] {
  return [...channels.keys()];
}

/**
 * Whether channels are currently suspended (page hidden ≥60s and/or offline).
 * Channels read this to avoid arming a retry the moment they are told to
 * suspend — see `_scheduleSubAgentReconnect`.
 */
export function isWebSocketSuspended(): boolean {
  return suspendReasons.size > 0;
}

/** Test/reset helper — drops every registration, listener and pending timer so
 *  specs start from a clean slate. NOT used by product code. */
export function _resetWebSocketOrchestrator(): void {
  channels.clear();
  suspendReasons.clear();
  removeListeners();
}
