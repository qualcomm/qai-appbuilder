// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * History / fork / checkpoints / channel-notify slice of
 * `useCodingSession` (cohesion split). Owns the history session list,
 * per-session history load, restore (optionally forking), file rewind to a
 * checkpoint, and WeChat/Feishu dual-channel notify binding. Reads/writes
 * the shared `CodingSessionContext`; `fetchSessions` (CRUD slice) is
 * injected because `restoreSession` refreshes the live session list.
 */
import type { Ref } from "vue";

import { apiJson, ApiError } from "@/api";
import { useToastStore } from "@/stores/toast";
import type { CodingSessionContext } from "./sessionContext";
import type { CodingMessage, HistorySession } from "../useCodingSession.types";

interface HistoryAllResponse {
  sessions: HistorySession[];
}

interface HistoryMessagesResponse {
  message_history?: Array<Record<string, unknown>>;
  messages?: Array<Record<string, unknown>>;
}

interface RestoreResponse {
  restored: boolean;
  forked: boolean;
}

export interface SessionHistorySlice {
  historySessions: Ref<HistorySession[]>;
  historyLoading: Ref<boolean>;
  loadHistorySessions: (includeClosed?: boolean, source?: string) => Promise<void>;
  loadSessionHistory: (sessionId: string) => Promise<void>;
  restoreSession: (sessionId: string, fork?: boolean) => Promise<boolean>;
  rewindFiles: (sessionId: string, checkpointId: string) => Promise<boolean>;
  setWechatNotify: (sessionId: string, wechatUserId: string | null) => Promise<void>;
  setFeishuNotify: (sessionId: string, feishuOpenId: string | null) => Promise<void>;
}

export function useSessionHistory(
  ctx: CodingSessionContext,
  deps: {
    /** Injected from the CRUD slice — restore refreshes the live list. */
    fetchSessions: () => Promise<void>;
  },
): SessionHistorySlice {
  const { st, prefix, t, ensureBucket, newId, toastError } = ctx;
  const { fetchSessions } = deps;

  /** Surface a success toast (4s) — used by the file-rewind outcome. */
  function toastSuccess(msg: string): void {
    useToastStore().push({
      id: crypto.randomUUID(),
      kind: "success",
      message: msg,
      timeoutMs: 4000,
    });
  }

  /** Load the history session list (V1 loadHistorySessions:1347). */
  async function loadHistorySessions(includeClosed = true, source?: string): Promise<void> {
    st.historyLoading.value = true;
    try {
      const params = new URLSearchParams();
      params.set("include_closed", includeClosed ? "true" : "false");
      if (source !== undefined && source !== "" && source !== "all") {
        params.set("source", source);
      }
      const res = await apiJson<HistoryAllResponse>(
        "GET",
        `${prefix}/sessions/history/all?${params.toString()}`,
      );
      st.historySessions.value = Array.isArray(res.sessions) ? res.sessions : [];
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : t("claudeCode.loadHistoryFailed", { msg: "" }));
    } finally {
      st.historyLoading.value = false;
    }
  }

  /**
   * Load a session's persisted message history into its bucket (V1
   * loadSessionHistory:226 / loadHistorySessionMessages:1621). Accepts both
   * the CC (`message_history`) and OC (`messages`) envelope keys.
   */
  async function loadSessionHistory(sessionId: string): Promise<void> {
    try {
      const res = await apiJson<HistoryMessagesResponse>(
        "GET",
        `${prefix}/sessions/${sessionId}/history`,
      );
      const raw = res.message_history ?? res.messages ?? [];
      const msgs: CodingMessage[] = raw.map((m) => {
        const role = (m["role"] as string | undefined) ?? "assistant";
        return {
          id: (m["id"] as string | undefined) ?? newId(),
          role: role === "user" ? "user" : role === "system" ? "system" : "assistant",
          content: (m["content"] as string | undefined) ?? "",
        };
      });
      st.sessionMessages[sessionId] = msgs;
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : t("claudeCode.loadHistoryFailed", { msg: "" }));
    }
  }

  /**
   * Restore a session, optionally forking a new branch (V1 restoreSession:1372).
   * On success the session is activated and its history reloaded.
   */
  async function restoreSession(sessionId: string, fork = false): Promise<boolean> {
    try {
      const res = await apiJson<RestoreResponse>(
        "POST",
        `${prefix}/sessions/${sessionId}/restore`,
        { fork },
      );
      if (res.restored) {
        st.isMode.value = true;
        st.activeSessionId.value = sessionId;
        ensureBucket(sessionId);
        await fetchSessions();
        await loadSessionHistory(sessionId);
        await loadHistorySessions();
      }
      return res.restored;
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : t("claudeCode.restoreSessionFailed", { msg: "" }));
      return false;
    }
  }

  /**
   * Rewind files to a per-message checkpoint (V1 rewindFiles:1748, V2 wire
   * `POST {prefix}/sessions/{id}/rewind {checkpoint_id}`). The user-row's
   * `checkpointId` (created at send-time) is forwarded so the call works
   * without leaking the V1 SDK concept into the UI layer. Requires
   * `enable_file_checkpointing` — when disabled the rewind button stays hidden.
   */
  async function rewindFiles(sessionId: string, checkpointId: string): Promise<boolean> {
    try {
      const res = await apiJson<{
        ok: boolean;
        session_id: string;
        checkpoint_id: string;
        removed: number;
        remaining: number;
        files_rewound?: boolean;
      }>("POST", `${prefix}/sessions/${sessionId}/rewind`, { checkpoint_id: checkpointId });
      // Reload the session message history so the bucket reflects the
      // rewound state (V1 reloaded the session after rewind).
      await loadSessionHistory(sessionId);
      // CC SDK file checkpoint/rewind (2-H3): when the SDK backend performed
      // a TRUE on-disk file restore the response carries `files_rewound:true`;
      // surface an accurate success toast (files restored vs messages only).
      if (res.files_rewound === true) {
        toastSuccess(t("claudeCode.rewindFilesDone", { msg: "Files restored to checkpoint" }));
      } else {
        toastSuccess(t("claudeCode.rewindMessagesDone", { msg: "Conversation rewound" }));
      }
      return true;
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : t("claudeCode.rewindFailed", { msg: "" }));
      return false;
    }
  }

  /** Bind WeChat dual-channel notifications (V1 setWechatNotify:1443). */
  async function setWechatNotify(sessionId: string, wechatUserId: string | null): Promise<void> {
    try {
      await apiJson("POST", `${prefix}/sessions/${sessionId}/wechat_notify`, {
        wechat_user_id: wechatUserId,
      });
      const s = st.sessions.value.find((x) => x.session_id === sessionId);
      if (s !== undefined) s.wechat_notify_user_id = wechatUserId;
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : t("claudeCode.setWechatNotifyFailed", { msg: "" }));
    }
  }

  /** Bind Feishu dual-channel notifications (V1 setFeishuNotify:1472). */
  async function setFeishuNotify(sessionId: string, feishuOpenId: string | null): Promise<void> {
    try {
      await apiJson("POST", `${prefix}/sessions/${sessionId}/feishu_notify`, {
        feishu_open_id: feishuOpenId,
      });
      const s = st.sessions.value.find((x) => x.session_id === sessionId);
      if (s !== undefined) s.feishu_notify_user_id = feishuOpenId;
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : t("claudeCode.setFeishuNotifyFailed", { msg: "" }));
    }
  }

  return {
    historySessions: st.historySessions,
    historyLoading: st.historyLoading,
    loadHistorySessions,
    loadSessionHistory,
    restoreSession,
    rewindFiles,
    setWechatNotify,
    setFeishuNotify,
  };
}
