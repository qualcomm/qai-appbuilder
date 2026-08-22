// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Streaming send + queue slice of `useCodingSession` (cohesion split).
 * Owns the two-step send (`POST .../messages → {stream_url}` then
 * `GET {stream_url}` SSE), the pending-message queue, the streaming locks,
 * and the inline-error helper. Reads/writes the shared
 * `CodingSessionContext`; the cross-cutting `refreshContextUsage` (context
 * slice) is injected so the post-turn REST refresh stays decoupled.
 */
import type { Ref } from "vue";

import { apiJson, apiSSE, ApiError } from "@/api";
import type { SseHandler } from "@/api";
import { dispatchFrame, formatErrorMessage, type FrameContext } from "./frameProcessing";
import { CC_MAX_QUEUE, type CodingSessionContext } from "./sessionContext";
import type {
  CodingMessage,
  CreateCheckpointResponse,
  QueuedMessage,
  SendMessageResponse,
} from "../useCodingSession.types";

export interface SessionStreamSlice {
  queue: Ref<QueuedMessage[]>;
  sendMessage: (text: string, forcedSessionId?: string) => Promise<void>;
  removeFromQueue: (itemId: string) => void;
  appendErrorToSession: (sessionId: string, msg: string) => void;
  stopStreaming: () => void;
}

export function useSessionStream(
  ctx: CodingSessionContext,
  deps: {
    /** Injected from the context slice — post-turn REST counter refresh. */
    refreshContextUsage: (sessionId: string, assistantMsg?: CodingMessage) => Promise<void>;
  },
): SessionStreamSlice {
  const { st, prefix, t, ensureBucket, newId, toastError } = ctx;
  const { refreshContextUsage } = deps;

  /** Append an inline ⚠️ error to a session's last assistant turn (V1 _appendErrorToSession). */
  function appendErrorToSession(sessionId: string, msg: string): void {
    const bucket = ensureBucket(sessionId);
    const last = [...bucket].reverse().find((m) => m.role === "assistant");
    if (last !== undefined) {
      last.isStreaming = false;
      last.content += (last.content === "" ? "" : "\n\n") + `⚠️ ${msg}`;
    } else {
      bucket.push({
        id: newId(),
        role: "system",
        content: `⚠️ ${msg}`,
        isWarning: true,
      });
    }
  }

  /** Release stream-level locks (V1 _stopStreaming). */
  function stopStreaming(): void {
    st.sendingLock = false;
    st.streaming.value = false;
    st.streamingSessionId.value = null;
  }

  /**
   * Process the next queued message (V1 _processNextInQueue:1225-1260).
   * Previews the head without shifting (avoids panel flicker), pre-locks,
   * pre-initialises progress, then shifts + sends on the next macrotask.
   */
  function processNextInQueue(): void {
    if (st.queue.value.length === 0) return;
    if (st.sendingLock) return;
    const next = st.queue.value[0];
    if (next === undefined) return;
    st.sendingLock = true;
    st.streaming.value = true;
    st.streamingSessionId.value = next.sessionId;
    st.sessionProgress[next.sessionId] = {
      stage: "start",
      detail: t("claudeCode.preparingToSend"),
      startTime: Date.now(),
    };
    setTimeout(() => {
      st.queue.value.shift();
      st.sendingLock = false;
      void sendMessage(next.text, next.sessionId);
    }, 0);
  }

  async function sendMessage(text: string, forcedSessionId?: string): Promise<void> {
    const sessionId = forcedSessionId ?? st.activeSessionId.value;
    if (sessionId === null) return;
    const trimmed = text.trim();
    if (trimmed === "") return;

    // Queue if this session is already streaming (V1 sendMessage:490-501).
    if (st.sendingLock && st.streamingSessionId.value === sessionId) {
      if (st.queue.value.length >= CC_MAX_QUEUE) {
        appendErrorToSession(sessionId, t("claudeCode.queueFullErr", { max: CC_MAX_QUEUE }));
        return;
      }
      st.queue.value.push({ id: Date.now().toString(), sessionId, text: trimmed });
      return;
    }

    const bucket = ensureBucket(sessionId);

    // Claim locks in V1 order: sync lock first, then reactive flags.
    st.sendingLock = true;
    st.streaming.value = true;
    st.streamingSessionId.value = sessionId;

    // Push user message + assistant placeholder. The user row's
    // `userMsgId` / `checkpointId` are filled in below once the V2
    // backend returns them (POST messages / POST checkpoint).
    const userMsg: CodingMessage = {
      id: newId(),
      role: "user",
      content: trimmed,
      timestamp: Date.now(),
    };
    bucket.push(userMsg);
    const assistant: CodingMessage = {
      id: newId(),
      role: "assistant",
      content: "",
      isStreaming: true,
      toolCalls: [],
      timestamp: Date.now(),
    };
    bucket.push(assistant);
    const turnStartedAt = Date.now();

    st.abortController?.abort();
    st.abortController = new AbortController();

    // Step 1: POST to obtain the per-message stream URL.
    let streamUrl: string;
    let userMsgId: string | undefined;
    try {
      const post = await apiJson<SendMessageResponse>(
        "POST",
        `${prefix}/sessions/${sessionId}/messages`,
        { message: trimmed },
      );
      streamUrl = post.stream_url;
      userMsgId = post.user_msg_id;
      userMsg.userMsgId = userMsgId;
      if (!streamUrl.startsWith("/") && !/^https?:\/\//i.test(streamUrl)) {
        streamUrl = `/${streamUrl}`;
      }
    } catch (e) {
      assistant.isStreaming = false;
      st.sessionProgress[sessionId] = null;
      stopStreaming();
      st.abortController = null;
      const msg = e instanceof ApiError ? e.message : t("claudeCode.sendMessageFailed", { msg: "" });
      appendErrorToSession(sessionId, msg);
      // Drain the queue so a transient failure doesn't strand pending messages.
      setTimeout(() => processNextInQueue(), 500);
      return;
    }

    // Step 1b (V2-only, V1 used SDK sdkUuid): create a checkpoint
    // labelled with the user_msg_id so the per-row ⏪ rewind button
    // can later POST `{checkpoint_id}`. Fire-and-forget: rewind is
    // best-effort and must not block streaming. Requires
    // `enable_file_checkpointing` — when disabled the call returns 4xx
    // and the rewind button simply stays hidden.
    if (userMsgId !== undefined) {
      void apiJson<CreateCheckpointResponse>(
        "POST",
        `${prefix}/sessions/${sessionId}/checkpoint`,
        { label: userMsgId },
      )
        .then((res) => {
          if (res.ok && res.checkpoint?.checkpoint_id !== undefined) {
            userMsg.checkpointId = res.checkpoint.checkpoint_id;
          }
        })
        .catch(() => {
          // checkpointing disabled / unsupported → leave checkpointId
          // empty so the rewind button stays hidden.
        });
    }

    // Step 2: consume the SSE stream, dispatch by `kind`. Per-frame
    // handling lives in the pure `coding/frameProcessing.ts` module;
    // final-turn book-keeping (token badge, duration_s, queue drain)
    // stays in onDone / finally below.
    const frameCtx: FrameContext = {
      t,
      sessionId,
      newId,
      setPendingPermission(pr) {
        st.pendingPermission.value = pr;
      },
      pendingPermissionSessionId() {
        return st.pendingPermission.value?.sessionId ?? null;
      },
      setSessionProgress(sid, progress) {
        st.sessionProgress[sid] = progress;
      },
    };
    const handler: SseHandler = {
      onMessage(data) {
        dispatchFrame(
          assistant,
          data as { kind?: string; sequence?: number; payload?: unknown },
          frameCtx,
        );
      },
      onError(err) {
        assistant.isStreaming = false;
        st.sessionProgress[sessionId] = null;
        // V1 parity: map the error code to a localized message and surface
        // it inline with a ⚠️ prefix; also toast for visibility.
        const code = err instanceof ApiError ? err.code : undefined;
        const text = formatErrorMessage(t, code, err.message || String(err));
        assistant.content += (assistant.content === "" ? "" : "\n\n") + `⚠️ ${text}`;
        toastError(text);
      },
      onDone() {
        // V2 wire: `event: done` is the single termination signal for the
        // assistant turn. Final book-keeping lives here, not in onMessage.
        assistant.isStreaming = false;
        for (const c of assistant.toolCalls ?? []) {
          if (c.status === "running") c.status = "done";
        }
        assistant.durationS = Math.round(((Date.now() - turnStartedAt) / 1000) * 10) / 10;
        st.sessionProgress[sessionId] = null;
        if (st.pendingPermission.value?.sessionId === sessionId) {
          st.pendingPermission.value = null;
        }
      },
    };

    try {
      await apiSSE(streamUrl, handler, {
        method: "GET",
        signal: st.abortController.signal,
      });
    } catch (e) {
      assistant.isStreaming = false;
      st.sessionProgress[sessionId] = null;
      if (e instanceof ApiError && e.type !== "AbortError") {
        toastError(e.message);
      }
    } finally {
      if (st.streamingSessionId.value === sessionId) {
        stopStreaming();
      }
      st.abortController = null;
      // V2 REST refresh: pull context counters (CC session-level + OC
      // per-turn) so the assistant token badge has data without coupling
      // to provider-specific frames. Counters carry the aggregate's REAL
      // cumulative usage (U-010 / 2-H2): the CodingSession aggregate tracks
      // token usage from provider usage frames via
      // StreamCodingSessionUseCase._record_frame_usage → record_token_usage.
      // A value of 0 is the initial state (no round streamed yet), not a stub.
      void refreshContextUsage(sessionId, assistant);
      // Drain any queued messages (V1 _processNextInQueue after done).
      processNextInQueue();
    }
  }

  /** Remove a queued message before it is sent (V1 removeFromCCQueue:1653). */
  function removeFromQueue(itemId: string): void {
    st.queue.value = st.queue.value.filter((i) => i.id !== itemId);
  }

  return {
    queue: st.queue,
    sendMessage,
    removeFromQueue,
    appendErrorToSession,
    stopStreaming,
  };
}
