// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Shared context for the `useCodingSession` sub-composables (cohesion split).
 *
 * `useCodingSession` was an "god composable" mixing five independent
 * responsibilities (session CRUD, streaming send + queue, permission,
 * context/effort, history/fork). Each is now a small factory that takes
 * this `CodingSessionContext` and contributes its slice. The shared
 * per-`kind` singleton state, the api prefix, the i18n translator and a
 * couple of cross-cutting helpers (bucket access, error toast, id gen)
 * live here so every slice reads/writes the SAME reactive state without
 * prop-drilling.
 *
 * This mirrors the pattern already used by `coding/frameProcessing.ts`
 * (passing a `FrameContext`): the composable is the orchestration layer,
 * the slices are pure factories.
 */
import { ref, reactive, type Ref } from "vue";
import { useI18n } from "vue-i18n";

import { useToastStore } from "@/stores/toast";
import type { Translator } from "./frameProcessing";
import type {
  CodingKind,
  CodingMessage,
  CodingSession,
  HistorySession,
  PermissionRequest,
  QueuedMessage,
  SessionProgress,
} from "../useCodingSession.types";

/** V1 CC_MAX_QUEUE (useClaudeCode.js:111). */
export const CC_MAX_QUEUE = 10;

export interface KindState {
  sessions: Ref<CodingSession[]>;
  activeSessionId: Ref<string | null>;
  isMode: Ref<boolean>;
  /**
   * Floating session-panel visibility (V1 `ccPanelOpen` / `ocPanelOpen`,
   * app.js:667/691). Decoupled from `isMode`: the panel's collapse (—)
   * button only hides the panel, while the pill's right-click / exit (✕)
   * leaves the mode. Re-clicking the pill toggles the panel back open.
   */
  panelOpen: Ref<boolean>;
  loading: Ref<boolean>;
  streaming: Ref<boolean>;
  streamingSessionId: Ref<string | null>;
  currentModel: Ref<string>;
  /** `{[sessionId]: CodingMessage[]}` — per-session isolation. */
  sessionMessages: Record<string, CodingMessage[]>;
  pendingPermission: Ref<PermissionRequest | null>;
  abortController: AbortController | null;
  sessionsLoaded: boolean;
  /** Pending message queue (V1 ccQueue, max CC_MAX_QUEUE). */
  queue: Ref<QueuedMessage[]>;
  /** Per-session progress indicator (V1 sessionProgress). */
  sessionProgress: Record<string, SessionProgress | null>;
  /** History session list (V1 historySessions). */
  historySessions: Ref<HistorySession[]>;
  historyLoading: Ref<boolean>;
  /**
   * Synchronous send lock (V1 `_sendingLock`): set before the first await so
   * it is observable earlier than the reactive `streaming` ref, eliminating
   * the concurrent-send race. Not a ref — never drives the UI.
   */
  sendingLock: boolean;
}

function makeKindState(): KindState {
  return {
    sessions: ref<CodingSession[]>([]),
    activeSessionId: ref<string | null>(null),
    isMode: ref(false),
    panelOpen: ref(false),
    loading: ref(false),
    streaming: ref(false),
    streamingSessionId: ref<string | null>(null),
    currentModel: ref(""),
    sessionMessages: reactive<Record<string, CodingMessage[]>>({}),
    pendingPermission: ref<PermissionRequest | null>(null),
    abortController: null,
    sessionsLoaded: false,
    queue: ref<QueuedMessage[]>([]),
    sessionProgress: reactive<Record<string, SessionProgress | null>>({}),
    historySessions: ref<HistorySession[]>([]),
    historyLoading: ref(false),
    sendingLock: false,
  };
}

const _stateByKind: Record<CodingKind, KindState> = {
  cc: makeKindState(),
  oc: makeKindState(),
};

function apiPrefix(kind: CodingKind): string {
  return kind === "cc" ? "/api/cc" : "/api/oc";
}

/** Shared context handed to every `useCodingSession` slice factory. */
export interface CodingSessionContext {
  kind: CodingKind;
  prefix: string;
  st: KindState;
  t: Translator;
  /** Lazily create + return a session's message bucket. */
  ensureBucket: (sessionId: string) => CodingMessage[];
  /** Stable client-side id (crypto.randomUUID). */
  newId: () => string;
  /** Surface an error toast (5s). */
  toastError: (msg: string) => void;
}

/**
 * Build the per-`kind` shared context. Called once by `useCodingSession`
 * inside a component setup (so `useI18n()` is valid); falls back to the
 * key itself if the i18n context is ever unavailable (out-of-setup unit
 * test call) so error mapping never throws.
 */
export function makeCodingSessionContext(kind: CodingKind): CodingSessionContext {
  const st = _stateByKind[kind];
  const prefix = apiPrefix(kind);

  let t: Translator;
  try {
    const i18n = useI18n();
    t = (key, named) => (named !== undefined ? i18n.t(key, named) : i18n.t(key));
  } catch {
    t = (key) => key;
  }

  function ensureBucket(sessionId: string): CodingMessage[] {
    if (st.sessionMessages[sessionId] === undefined) {
      st.sessionMessages[sessionId] = [];
    }
    return st.sessionMessages[sessionId]!;
  }

  function newId(): string {
    return crypto.randomUUID();
  }

  function toastError(msg: string): void {
    useToastStore().push({
      id: crypto.randomUUID(),
      kind: "error",
      message: msg,
      timeoutMs: 5000,
    });
  }

  return { kind, prefix, st, t, ensureBucket, newId, toastError };
}
