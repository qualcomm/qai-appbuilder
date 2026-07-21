// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Chat Tools API client.
 *
 * Covers:
 *   GET /api/chat/tools — list chat-registered tools (live registry)
 *
 * The endpoint surfaces the LIVE chat-tool registry so the Discussion role
 * allowlist UI (and any future "what can this role do?" inspector) can render
 * the available tool set without hard-coding the list in the front-end build
 * — when a new tool is registered in the back-end, it appears automatically.
 *
 * Ordered by the back-end's canonical ``TOOL_ORDER`` (read / edit / write /
 * apply_patch / exec / glob / grep / webfetch / agent / list_subagents /
 * todowrite / question / appbuilder_run / appbuilder_batch_run) — the single
 * source of truth for tool display order across all surfaces.
 */

import { apiJson, type ApiRequestOptions } from "./http";

/** One tool descriptor returned by ``GET /api/chat/tools``. */
export interface ChatToolDescriptor {
  /** Tool name (registry key). */
  name: string;
  /** One-line tool description (verbatim from the LLM function-call schema). */
  description: string;
  /**
   * ``true`` when this tool may be exposed to a discussion speaker (the
   * speaker still must list it in ``allowed_tools``). ``false`` for tools
   * the back-end hard-blocks for discussions.
   */
  available_in_discussion: boolean;
  /**
   * ``true`` for mode-conditional tools (``appbuilder_run`` /
   * ``appbuilder_batch_run``) that only register schemas in certain tool modes.
   */
  conditional: boolean;
}

/** Response shape of ``GET /api/chat/tools``. */
export interface ListChatToolsResponse {
  tools: ChatToolDescriptor[];
}

/**
 * Fetch the live chat-tool catalogue.
 *
 * Returns the back-end's tool list in canonical display order; callers may
 * iterate / filter as needed.
 */
export async function fetchChatTools(
  opts?: ApiRequestOptions,
): Promise<ListChatToolsResponse> {
  return apiJson<ListChatToolsResponse>(
    "GET",
    "/api/chat/tools",
    undefined,
    opts,
  );
}
