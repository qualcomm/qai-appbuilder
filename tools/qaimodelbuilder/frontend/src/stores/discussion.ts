// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Multi-Agent discussion store (block-5).
 *
 * Thin CRUD client for the conversation-scoped discussion config + named
 * participant registry. Wraps the block-4 backend routes:
 *
 *   GET    /api/chat/conversations/{id}/participants
 *   POST   /api/chat/conversations/{id}/participants
 *   PATCH  /api/chat/conversations/{id}/participants/{pid}
 *   DELETE /api/chat/conversations/{id}/participants/{pid}
 *   GET    /api/chat/conversations/{id}/discussion
 *   PATCH  /api/chat/conversations/{id}/discussion
 *
 * The store is intentionally STATELESS beyond a tiny per-conversation cache —
 * the authoritative reactive copy of a tab's discussion config lives on
 * `chatTabs.tab.discussion` (so the panel, the SSE qs, and the frame handlers
 * all read one place). `useDiscussion` is the composable that binds this store
 * to the active tab; this store only owns the wire shapes + HTTP calls.
 *
 * Wire shapes mirror the block-4 contract verbatim (snake_case); the store maps
 * them to/from the front-end `DiscussionParticipant` / `DiscussionConfig`
 * camelCase view models (`_chatTabsTypes.ts`).
 */
import { defineStore } from "pinia";
import { apiJson } from "@/api";
import type {
  DiscussionConfig,
  DiscussionParticipant,
  SelectorMode,
} from "./_chatTabsTypes";

// ---------------------------------------------------------------------------
// Wire shapes (block-4 contract — snake_case)
// ---------------------------------------------------------------------------

interface ParticipantConfigWire {
  allowed_tools?: string[] | null;
  enabled_skills?: string[] | null;
  color?: number | null;
}

interface ParticipantWire {
  id: string;
  display_name: string;
  model_id?: string | null;
  persona?: string | null;
  config?: ParticipantConfigWire | null;
}

/** Shape of `GET /conversations/{id}/participants` — the backend wraps the
 *  list in `{ items: [...] }` (`ParticipantListResponse`), consistent with the
 *  other list endpoints (conversations / models). This MUST be parsed via
 *  `.items`; treating the response as a bare array silently drops every
 *  participant on reload (the object is not `Array.isArray`). */
interface ParticipantListWire {
  items?: ParticipantWire[] | null;
}

interface DiscussionConfigWire {
  is_discussion?: boolean;
  selector_mode?: string;
  max_rounds?: number;
  enable_judge?: boolean;
  discussion_prompt?: string | null;
  selected_mode_id?: string | null;
  mode_selection_policy?: string | null;
  // DISC-2 §22A.8 convergence-control flags (tail-appended, optional). Absent
  // key ⇒ OFF for the bools / "conservative" for the mode (legacy untouched).
  convergence_control_enabled?: boolean;
  manager_early_end_enabled?: boolean;
  soft_stop_enabled?: boolean;
  soft_stop_mode?: string | null;
  // DISC-2 P4-step1 §22A.7 social-path response policy (tail-appended,
  // optional). Absent/illegal key ⇒ "single_brief_reply" (phase-1 behaviour).
  social_response_policy?: string | null;
  // DISC-2 P4-step2 §22A.7 Manager prompt customization (tail-appended,
  // optional). The UI sends only `manager_prompt_append`; a non-empty value
  // implies the backend "append_instruction" mode (inferred at read time).
  manager_prompt_customization_mode?: string | null;
  manager_prompt_append?: string | null;
  // DISC-1 §22.7 / DISC-2 §22A.5 feature flags (tail-appended, optional).
  // Absent key ⇒ OFF (legacy conversations untouched); a fresh tab seeds these
  // ON when the user enables discussion (see useDiscussion.setDiscussionEnabled).
  implementation_enabled?: boolean;
  intent_classifier_enabled?: boolean;
  // DISC-1 TODO-2 user-tunable numeric/string knobs (tail-appended, optional).
  // Absent ⇒ the backend constant default at read time.
  impl_max_total_file_edits?: number;
  impl_max_total_exec_calls?: number;
  impl_max_total_runtime_seconds?: number;
  impl_max_total_changed_files?: number;
  soft_stop_similarity?: number;
  soft_stop_min_rounds?: number;
  soft_stop_consecutive_turns?: number;
  intent_classifier_model?: string | null;
  intent_classifier_timeout_ms?: number;
  implementation_planner_model?: string | null;
  implementation_planner_timeout_ms?: number;
  // DISC-1 三期-step5 + 完成判定 B validator / verify-command knobs.
  implementation_validator_enabled?: boolean;
  implementation_validator_timeout_ms?: number;
  implementation_verify_command_timeout_ms?: number;
}

/** Body for create / update participant (the `id` is route/response-only). */
export interface ParticipantInput {
  display_name: string;
  model_id?: string;
  persona?: string;
  config: {
    allowed_tools: string[];
    enabled_skills?: string[];
    color?: number;
  };
}

// ---------------------------------------------------------------------------
// Wire ↔ view-model mappers
// ---------------------------------------------------------------------------

function wireToParticipant(w: ParticipantWire): DiscussionParticipant {
  return {
    id: w.id,
    display_name: w.display_name,
    ...(w.model_id != null && w.model_id !== "" ? { model_id: w.model_id } : {}),
    ...(w.persona != null && w.persona !== "" ? { persona: w.persona } : {}),
    config: {
      allowed_tools: Array.isArray(w.config?.allowed_tools)
        ? [...(w.config?.allowed_tools as string[])]
        : [],
      enabled_skills: Array.isArray(w.config?.enabled_skills)
        ? [...(w.config?.enabled_skills as string[])]
        : [],
      ...(typeof w.config?.color === "number"
        ? { color: w.config?.color }
        : {}),
    },
  };
}

function participantInputToWire(input: ParticipantInput): Record<string, unknown> {
  return {
    display_name: input.display_name,
    ...(input.model_id !== undefined && input.model_id !== ""
      ? { model_id: input.model_id }
      : {}),
    ...(input.persona !== undefined && input.persona !== ""
      ? { persona: input.persona }
      : {}),
    config: {
      allowed_tools: input.config.allowed_tools,
      enabled_skills: input.config.enabled_skills ?? [],
      ...(input.config.color !== undefined ? { color: input.config.color } : {}),
    },
  };
}

function normaliseSelectorMode(raw: string | undefined): SelectorMode {
  return raw === "round_robin" ? "round_robin" : "manager";
}

/** Coerce a wire value to a finite number, falling back to ``fallback`` for a
 *  missing / non-finite value (so the panel always shows a sensible default). */
function _num(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value)
    ? value
    : fallback;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const useDiscussionStore = defineStore("discussion", () => {
  /** Fetch the full discussion config + participant registry for a conversation.
   *  Combines the two GET routes into one front-end `DiscussionConfig`. */
  async function fetchConfig(conversationId: string): Promise<DiscussionConfig> {
    const [cfg, participantsResp] = await Promise.all([
      apiJson<DiscussionConfigWire>(
        "GET",
        `/api/chat/conversations/${encodeURIComponent(conversationId)}/discussion`,
      ),
      apiJson<ParticipantListWire>(
        "GET",
        `/api/chat/conversations/${encodeURIComponent(conversationId)}/participants`,
      ),
    ]);
    // The participants endpoint returns `{ items: [...] }` (ParticipantListResponse),
    // NOT a bare array. Read `.items` here — a previous bare-array assumption made
    // `Array.isArray()` fail on the wrapper object and silently dropped every saved
    // role on reload (roles "disappeared" after a restart even though the DB kept
    // them). Keep the defensive guard, but on `.items`.
    const participantItems = Array.isArray(participantsResp?.items)
      ? participantsResp.items
      : [];
    return {
      isDiscussion: cfg.is_discussion === true,
      selectorMode: normaliseSelectorMode(cfg.selector_mode),
      maxRounds:
        typeof cfg.max_rounds === "number" && cfg.max_rounds > 0
          ? cfg.max_rounds
          : 6,
      enableJudge: cfg.enable_judge !== false,
      ...(typeof cfg.discussion_prompt === "string" && cfg.discussion_prompt !== ""
        ? { discussionPrompt: cfg.discussion_prompt }
        : {}),
      ...(typeof cfg.selected_mode_id === "string" && cfg.selected_mode_id !== ""
        ? { selectedModeId: cfg.selected_mode_id }
        : {}),
      ...(typeof cfg.mode_selection_policy === "string" &&
      cfg.mode_selection_policy !== ""
        ? { modeSelectionPolicy: cfg.mode_selection_policy }
        : {}),
      // DISC-2 §22A.8: an ABSENT key means OFF (legacy conversations are
      // untouched), so bools default false via `=== true`; soft-stop mode
      // defaults to "conservative" when absent / blank.
      convergenceControlEnabled: cfg.convergence_control_enabled === true,
      managerEarlyEndEnabled: cfg.manager_early_end_enabled === true,
      softStopEnabled: cfg.soft_stop_enabled === true,
      softStopMode:
        typeof cfg.soft_stop_mode === "string" && cfg.soft_stop_mode !== ""
          ? cfg.soft_stop_mode
          : "conservative",
      socialResponsePolicy:
        typeof cfg.social_response_policy === "string" &&
        cfg.social_response_policy !== ""
          ? cfg.social_response_policy
          : "single_brief_reply",
      managerPromptAppend:
        typeof cfg.manager_prompt_append === "string"
          ? cfg.manager_prompt_append
          : "",
      // DISC-1 §22.7 / DISC-2 §22A.5: an ABSENT key means OFF (legacy
      // conversations untouched), so bools default false via `=== true`.
      implementationEnabled: cfg.implementation_enabled === true,
      intentClassifierEnabled: cfg.intent_classifier_enabled === true,
      // DISC-1 TODO-2 tunable knobs: an absent key shows the UI default (= the
      // backend constant default), so the panel always renders a sensible
      // number; a non-number is ignored.
      implMaxTotalFileEdits: _num(cfg.impl_max_total_file_edits, 80),
      implMaxTotalExecCalls: _num(cfg.impl_max_total_exec_calls, 120),
      implMaxTotalRuntimeSeconds: _num(
        cfg.impl_max_total_runtime_seconds,
        1800,
      ),
      implMaxTotalChangedFiles: _num(cfg.impl_max_total_changed_files, 60),
      softStopSimilarity: _num(cfg.soft_stop_similarity, 0.72),
      softStopMinRounds: _num(cfg.soft_stop_min_rounds, 3),
      softStopConsecutiveTurns: _num(cfg.soft_stop_consecutive_turns, 2),
      intentClassifierModel:
        typeof cfg.intent_classifier_model === "string"
          ? cfg.intent_classifier_model
          : "",
      intentClassifierTimeoutMs: _num(cfg.intent_classifier_timeout_ms, 2000),
      implementationPlannerModel:
        typeof cfg.implementation_planner_model === "string"
          ? cfg.implementation_planner_model
          : "",
      implementationPlannerTimeoutMs: _num(
        cfg.implementation_planner_timeout_ms,
        8000,
      ),
      // DISC-1 三期-step5 + 完成判定 B: validator OFF by default (=== true);
      // timeouts show the backend constant default when absent.
      implementationValidatorEnabled:
        cfg.implementation_validator_enabled === true,
      implementationValidatorTimeoutMs: _num(
        cfg.implementation_validator_timeout_ms,
        8000,
      ),
      implementationVerifyCommandTimeoutMs: _num(
        cfg.implementation_verify_command_timeout_ms,
        120000,
      ),
      participants: participantItems.map(wireToParticipant),
    };
  }

  /** PATCH the conversation-level discussion settings (selector/rounds/judge/
   *  on-off). Only the provided keys are sent. */
  async function patchConfig(
    conversationId: string,
    patch: Partial<
      Pick<
        DiscussionConfig,
        | "isDiscussion"
        | "selectorMode"
        | "maxRounds"
        | "enableJudge"
        | "discussionPrompt"
        | "selectedModeId"
        | "modeSelectionPolicy"
        | "convergenceControlEnabled"
        | "managerEarlyEndEnabled"
        | "softStopEnabled"
        | "softStopMode"
        | "socialResponsePolicy"
        | "managerPromptAppend"
        | "implementationEnabled"
        | "intentClassifierEnabled"
        | "implMaxTotalFileEdits"
        | "implMaxTotalExecCalls"
        | "implMaxTotalRuntimeSeconds"
        | "implMaxTotalChangedFiles"
        | "softStopSimilarity"
        | "softStopMinRounds"
        | "softStopConsecutiveTurns"
        | "intentClassifierModel"
        | "intentClassifierTimeoutMs"
        | "implementationPlannerModel"
        | "implementationPlannerTimeoutMs"
        | "implementationValidatorEnabled"
        | "implementationValidatorTimeoutMs"
        | "implementationVerifyCommandTimeoutMs"
      >
    >,
  ): Promise<void> {
    const body: Record<string, unknown> = {};
    if (patch.isDiscussion !== undefined) body.is_discussion = patch.isDiscussion;
    if (patch.selectorMode !== undefined) body.selector_mode = patch.selectorMode;
    if (patch.maxRounds !== undefined) body.max_rounds = patch.maxRounds;
    if (patch.enableJudge !== undefined) body.enable_judge = patch.enableJudge;
    if (patch.discussionPrompt !== undefined)
      body.discussion_prompt = patch.discussionPrompt;
    if (patch.selectedModeId !== undefined)
      body.selected_mode_id = patch.selectedModeId;
    if (patch.modeSelectionPolicy !== undefined)
      body.mode_selection_policy = patch.modeSelectionPolicy;
    if (patch.convergenceControlEnabled !== undefined)
      body.convergence_control_enabled = patch.convergenceControlEnabled;
    if (patch.managerEarlyEndEnabled !== undefined)
      body.manager_early_end_enabled = patch.managerEarlyEndEnabled;
    if (patch.softStopEnabled !== undefined)
      body.soft_stop_enabled = patch.softStopEnabled;
    if (patch.softStopMode !== undefined)
      body.soft_stop_mode = patch.softStopMode;
    if (patch.socialResponsePolicy !== undefined)
      body.social_response_policy = patch.socialResponsePolicy;
    // DISC-2 P4-step2: the UI sends only the append text; the backend infers
    // "append_instruction" when it is non-empty (front-end simplification).
    if (patch.managerPromptAppend !== undefined)
      body.manager_prompt_append = patch.managerPromptAppend;
    if (patch.implementationEnabled !== undefined)
      body.implementation_enabled = patch.implementationEnabled;
    if (patch.intentClassifierEnabled !== undefined)
      body.intent_classifier_enabled = patch.intentClassifierEnabled;
    if (patch.implMaxTotalFileEdits !== undefined)
      body.impl_max_total_file_edits = patch.implMaxTotalFileEdits;
    if (patch.implMaxTotalExecCalls !== undefined)
      body.impl_max_total_exec_calls = patch.implMaxTotalExecCalls;
    if (patch.implMaxTotalRuntimeSeconds !== undefined)
      body.impl_max_total_runtime_seconds = patch.implMaxTotalRuntimeSeconds;
    if (patch.implMaxTotalChangedFiles !== undefined)
      body.impl_max_total_changed_files = patch.implMaxTotalChangedFiles;
    if (patch.softStopSimilarity !== undefined)
      body.soft_stop_similarity = patch.softStopSimilarity;
    if (patch.softStopMinRounds !== undefined)
      body.soft_stop_min_rounds = patch.softStopMinRounds;
    if (patch.softStopConsecutiveTurns !== undefined)
      body.soft_stop_consecutive_turns = patch.softStopConsecutiveTurns;
    if (patch.intentClassifierModel !== undefined)
      body.intent_classifier_model = patch.intentClassifierModel;
    if (patch.intentClassifierTimeoutMs !== undefined)
      body.intent_classifier_timeout_ms = patch.intentClassifierTimeoutMs;
    if (patch.implementationPlannerModel !== undefined)
      body.implementation_planner_model = patch.implementationPlannerModel;
    if (patch.implementationPlannerTimeoutMs !== undefined)
      body.implementation_planner_timeout_ms =
        patch.implementationPlannerTimeoutMs;
    if (patch.implementationValidatorEnabled !== undefined)
      body.implementation_validator_enabled =
        patch.implementationValidatorEnabled;
    if (patch.implementationValidatorTimeoutMs !== undefined)
      body.implementation_validator_timeout_ms =
        patch.implementationValidatorTimeoutMs;
    if (patch.implementationVerifyCommandTimeoutMs !== undefined)
      body.implementation_verify_command_timeout_ms =
        patch.implementationVerifyCommandTimeoutMs;
    await apiJson<DiscussionConfigWire>(
      "PATCH",
      `/api/chat/conversations/${encodeURIComponent(conversationId)}/discussion`,
      body,
    );
  }

  /** Create a new named participant; returns the persisted view model. */
  async function createParticipant(
    conversationId: string,
    input: ParticipantInput,
  ): Promise<DiscussionParticipant> {
    const w = await apiJson<ParticipantWire>(
      "POST",
      `/api/chat/conversations/${encodeURIComponent(conversationId)}/participants`,
      participantInputToWire(input),
    );
    return wireToParticipant(w);
  }

  /** Update an existing participant; returns the updated view model. */
  async function updateParticipant(
    conversationId: string,
    participantId: string,
    input: ParticipantInput,
  ): Promise<DiscussionParticipant> {
    const w = await apiJson<ParticipantWire>(
      "PATCH",
      `/api/chat/conversations/${encodeURIComponent(conversationId)}/participants/${encodeURIComponent(participantId)}`,
      participantInputToWire(input),
    );
    return wireToParticipant(w);
  }

  /** Delete a participant. */
  async function deleteParticipant(
    conversationId: string,
    participantId: string,
  ): Promise<void> {
    await apiJson(
      "DELETE",
      `/api/chat/conversations/${encodeURIComponent(conversationId)}/participants/${encodeURIComponent(participantId)}`,
    );
  }

  /** Create an empty conversation (used to back a fresh tab when the user
   *  configures discussion / adds a participant BEFORE sending the first
   *  message). Returns the raw backend summary (id + title + counts) so the
   *  caller can bind it to the tab and upsert the sidebar. */
  async function createConversation(
    title: string,
  ): Promise<Record<string, unknown>> {
    return await apiJson<Record<string, unknown>>(
      "POST",
      "/api/chat/conversations",
      { title },
    );
  }

  return {
    fetchConfig,
    patchConfig,
    createParticipant,
    updateParticipant,
    deleteParticipant,
    createConversation,
  };
});
