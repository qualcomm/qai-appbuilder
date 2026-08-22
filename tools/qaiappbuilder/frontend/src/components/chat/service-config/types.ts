// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Shared TypeScript interfaces for the ServiceConfigPanel split-out Tab
 * components. These mirror the ``service_config.json`` document shape consumed
 * via ``GET/POST /api/config`` and the ``service_launch`` section of the
 * forge-config document.
 */

export interface ForgeConfigResponse {
  config: Record<string, unknown>;
}

export interface ServiceConfigResponse {
  config: ServiceConfig;
  meta: { using_default_config: boolean; config_file_path: string };
}

export interface ModelSlot {
  name: string;
  context_size: number;
  enabled: boolean;
  backend: string;
  device: string;
  /**
   * On-disk model directory. V1 (useConfig.js:161-164) forces `path === name`
   * for the 3 fixed slots on save so the backend resolves the model dir.
   */
  path?: string;
}

/**
 * Per-format buckets of available local model names. Backed by
 * ``/api/service/models`` (each entry tagged with its ``format``
 * field — see ``interfaces/http/routes/model_runtime.py:283-301``)
 * and bucketed client-side in ``ServiceConfigPanel.vue``.
 *
 * V1 (``ServiceConfigPanel.js:205/229/253``) renders the per-slot
 * select option list from the same per-format buckets
 * (``localModelsByFmt.qnn`` / ``.gguf`` / ``.mnn``) so the slots
 * only show models whose on-disk format matches the slot's
 * runtime backend (NPU=QNN, GPU=GGUF, CPU=MNN).
 */
export interface LocalModelsByFormat {
  qnn: string[];
  gguf: string[];
  mnn: string[];
}

export interface CloudModel {
  enabled: boolean;
  base_url: string;
  api_key: string;
  model: string;
  context_size: number;
  require_desensitize?: boolean;
  upload_policy?: { enable_sensitivity_check: boolean; enable_desensitization: boolean };
  endpoints: unknown[];
}

export interface CloudShared {
  timeout_seconds: number;
  stream_timeout_seconds: number;
  log_debug: boolean;
  retry: { max: number; backoff_ms: number; max_total_attempts: number; retry_on_429_switch_endpoint: boolean };
  circuit_breaker: { failure_threshold: number; cooldown_seconds: number };
  rate_limit: { max_inferences_per_task: number; max_tokens_per_task: number };
}

export interface FallbackLocalUnavailable {
  s0: string;
  s1: string;
  s2: string;
}

export interface Routing {
  enabled: boolean;
  prefer_local_for_simple: boolean;
  /** V1 (ServiceConfigPanel.js:445) wires the enterprise-cloud "require
      desensitize (S1)" toggle to `routing.enterprise_cloud_require_desensitize`
      — NOT to `enterprise_cloud_model.require_desensitize`. The backend
      consumes `service_config.json` verbatim, so this is the wire key the
      service reads. */
  enterprise_cloud_require_desensitize?: boolean;
  fallback: {
    /** V1 schema has no fallback.enabled / .strategy — these are V2-only and
        will be dropped from defaults in helpers.ts; declared optional here so
        existing 7-tab template references typecheck during the transition.
        Subsequent tab-alignment PRs remove the references and these fields. */
    enabled?: boolean;
    strategy?: string;
    cloud_unavailable_to_local: boolean;
    clean_local_history_on_fallback: boolean;
    local_unavailable: FallbackLocalUnavailable;
    max_input_overflow_retries: number;
    enterprise_cloud_unavailable: string;
    public_cloud_unavailable: string;
  };
  agent_routing: {
    /** V1 schema has no agent_routing.enabled — declared optional during
        the 7-tab field-alignment transition (see fallback.enabled note). */
    enabled?: boolean;
    sub_agent_prefer_local: boolean;
    sub_agent_allow_cloud_on_c2: boolean;
    max_tool_call_retries: number;
  };
  sticky_routing: { enabled: boolean; ttl_seconds: number; max_sessions: number };
  incremental_check: {
    enabled: boolean;
    session_ttl_seconds: number;
    max_sessions: number;
    s2_always_full_check: boolean;
    detect_sensitive_reference: boolean;
    detect_history_tampering: boolean;
  };
  s2_turn_cleaning: {
    enabled: boolean;
    log_details: boolean;
    allow_cloud_reroute_after_clean: boolean;
  };
  metrics: {
    /** V1 schema has no metrics.enabled — declared optional during the
        7-tab field-alignment transition (see fallback.enabled note). */
    enabled?: boolean;
    summary_every_n_requests: number;
    summary_every_seconds: number;
  };
  cache: { ttl_seconds: number; max_entries: number };
  sensitivity_detection?: SensitivityDetection;
  desensitization?: Desensitization;
  complexity?: ComplexityAssessment;
}

export interface DetectionRules {
  [key: string]: boolean | string;
  enable_phone: boolean; level_phone: string;
  enable_email: boolean; level_email: string;
  enable_id_card: boolean; level_id_card: string;
  enable_bank_card: boolean; level_bank_card: string;
  enable_api_key: boolean; level_api_key: string;
  enable_private_key: boolean; level_private_key: string;
  enable_token: boolean; level_token: string;
  enable_password: boolean; level_password: string;
}

export interface ExtendedRules {
  enable_local_path: boolean;
  enable_internal_url: boolean;
  enable_device_id: boolean;
  enable_image_data: boolean;
}

export interface SensitivityDetection {
  enabled: boolean;
  method: string;
  use_local_model_fallback: boolean;
  strict_s2_union: boolean;
  timeout_ms: number;
  model_input_max_chars: number;
  max_gen_tokens: number;
  debug_log_matches: boolean;
  keywords_dict_path: string;
  keywords_reload_interval_seconds: number;
  detection_rules: DetectionRules;
  extended_rules: ExtendedRules;
}

export interface DesensEntitySwitches {
  [key: string]: boolean;
  enable_phone: boolean; enable_email: boolean; enable_id_card: boolean;
  enable_bank_card: boolean; enable_api_key: boolean; enable_private_key: boolean;
  enable_token: boolean; enable_password: boolean; enable_internal_url: boolean;
  enable_local_path: boolean; enable_device_id: boolean; enable_image_data: boolean;
}

export interface Desensitization {
  enabled: boolean;
  /** V1 service_config.json represents this as a JSON array of strategy
      identifiers (e.g. ``["structured_placeholder"]``). The legacy V2
      default was a bare string; helpers.ts now seeds the array shape and
      this type is tightened to ``string[]`` so the wire format matches V1
      byte-for-byte.

      ⚠️ Wave 3 follow-up: ServiceConfigSecurityTab.vue:247 still has
      ``v-model="desensitization(cfg).strategies"`` against a plain
      ``<input type="text">``. The next agent must replace that with either
      a multi-select / checkbox list of known strategy identifiers, or a
      comma-separated string ↔ array converter (mirroring
      ``allowedToolsToText`` / ``textToAllowedTools`` in helpers.ts). */
  strategies: string[];
  format_preserving_enabled: boolean;
  restore_response_enabled: boolean;
  restore_stream_enabled: boolean;
  iterative: boolean;
  max_rounds: number;
  log_desensitization_details: boolean;
  entity_switches: DesensEntitySwitches;
}

export interface ComplexityAssessment {
  method: string;
  use_local_model_fallback: boolean;
  timeout_ms: number;
  model_input_max_chars: number;
  thresholds: { tool_calls: number };
  keywords_c1?: string[];
  keywords_c2?: string[];
}

export interface LongTextSumCache {
  enabled: boolean;
  max_entries: number;
  max_memory_mb: number;
  ttl_minutes: number;
}

export interface LongTextSummarization {
  enabled: boolean;
  trigger_ratio: number;
  chunk_ratio: number;
  max_chunks: number;
  summarize_user_messages: boolean;
  summarize_tool_responses: boolean;
  verbose_logging: boolean;
  cache: LongTextSumCache;
}

export interface EmergencyTruncation {
  enabled: boolean;
  max_truncation_ratio: number;
  safety_margin_tokens: number;
}

export interface PromptOptimization {
  allowed_tools: string[];
  skill_catalog_format: string;
  enable_skill_auto_correction: boolean;
  enable_tool_whitelist: boolean;
  tool_call_temperature: number;
  spawn_guard: { enabled: boolean };
  max_messages_limit: number;
  recent_window: number;
  output_reserve_ratio: number;
  old_compress_len: number;
  recent_compress_len: number;
  tool_compress_len: number;
  min_compress_threshold: number;
  tool_min_length: number;
  emergency_truncation: EmergencyTruncation;
  long_text_summarization: LongTextSummarization;
  system_prompts?: {
    sections_enabled?: {
      critical_rule?: boolean;
      tools_intro?: boolean;
      catalog_structured_intro?: boolean;
    };
    few_shot_examples_enabled?: {
      enabled?: boolean;
      skill_correct_call?: boolean;
      no_skill_needed?: boolean;
      max_skill_examples?: number;
    };
  };
}

export interface ServiceDebug {
  status_update_content_visible: boolean;
  log_rule_matches: boolean;
  log_inference_stream: boolean;
}

export interface ServiceConfig {
  local_model?: { enabled: boolean };
  default_model?: string;
  models?: ModelSlot[];
  cloud_shared?: CloudShared;
  cloud_model?: CloudModel;
  enterprise_cloud_model?: CloudModel;
  routing?: Routing;
  prompt_optimization?: PromptOptimization;
  debug?: ServiceDebug;
}
