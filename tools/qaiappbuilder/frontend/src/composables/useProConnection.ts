// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import { reactive } from "vue";

import { apiJson } from "@/api";

interface SessionStateResponse {
  connected: boolean;
  session_id?: string | null;
  agent_url?: string | null;
  insecure: boolean;
}

interface ConnectRequest {
  tab_id: string;
  agent_url?: string | null;
  session_id?: string | null;
  insecure?: boolean | null;
  conversation_id?: string | null;
}

interface ConnectionState {
  connected: boolean;
  sessionId: string | null;
  agentUrl: string | null;
}

const connections = reactive(new Map<string, ConnectionState>());

function stateFor(tabId: string): ConnectionState {
  let state = connections.get(tabId);
  if (state === undefined) {
    state = { connected: false, sessionId: null, agentUrl: null };
    connections.set(tabId, state);
  }
  return state;
}

function applyState(tabId: string, response: SessionStateResponse): void {
  const state = stateFor(tabId);
  state.connected = response.connected;
  state.sessionId = response.session_id ?? null;
  state.agentUrl = response.agent_url ?? null;
}

async function connect(
  request: ConnectRequest,
): Promise<SessionStateResponse> {
  const response = await apiJson<SessionStateResponse, ConnectRequest>(
    "POST",
    "/api/mb-pro-session/connect",
    request,
  );
  applyState(request.tab_id, response);
  return response;
}

async function disconnect(tabId: string): Promise<SessionStateResponse> {
  const response = await apiJson<SessionStateResponse, { tab_id: string }>(
    "POST",
    "/api/mb-pro-session/disconnect",
    { tab_id: tabId },
  );
  applyState(tabId, response);
  return response;
}

async function refresh(tabId: string): Promise<SessionStateResponse> {
  const response = await apiJson<SessionStateResponse>(
    "GET",
    "/api/mb-pro-session/state",
    undefined,
    { query: { tab_id: tabId } },
  );
  applyState(tabId, response);
  return response;
}

export function useProConnection() {
  return {
    isConnectedFor: (tabId: string): boolean => stateFor(tabId).connected,
    agentUrlFor: (tabId: string): string | null => stateFor(tabId).agentUrl,
    sessionIdFor: (tabId: string): string | null => stateFor(tabId).sessionId,
    connect,
    disconnect,
    refresh,
  };
}
