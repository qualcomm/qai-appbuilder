// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Pure lazy-initialisation + string-conversion helpers for the
 * ServiceConfigPanel Tab components.
 *
 * Each accessor takes the reactive ``ServiceConfig`` object (the unwrapped
 * ``svcCfg`` ref value, passed down as the ``cfg`` prop) and lazily fills in
 * nested objects with V1-parity defaults, returning the (now guaranteed)
 * sub-object so templates can ``v-model`` into it. Mutating nested properties
 * of the reactive object preserves reactivity, so children share the single
 * source of truth held by the parent.
 */
import type {
  CloudModel,
  CloudShared,
  ComplexityAssessment,
  Desensitization,
  ModelSlot,
  PromptOptimization,
  Routing,
  SensitivityDetection,
  ServiceConfig,
  ServiceDebug,
} from "./types";

export function localModel(cfg: ServiceConfig): { enabled: boolean } {
  if (!cfg.local_model) cfg.local_model = { enabled: true };
  return cfg.local_model;
}

export function modelSlot(cfg: ServiceConfig, idx: number): ModelSlot {
  if (!Array.isArray(cfg.models)) cfg.models = [];
  while (cfg.models.length <= idx) {
    cfg.models.push({ name: "", context_size: 4096, enabled: false, backend: "", device: "" });
  }
  return cfg.models[idx]!;
}

export function cloudModel(cfg: ServiceConfig): CloudModel {
  if (!cfg.cloud_model) {
    cfg.cloud_model = { enabled: true, base_url: "", api_key: "", model: "", context_size: 1000000, endpoints: [] };
  }
  return cfg.cloud_model;
}

export function enterpriseCloudModel(cfg: ServiceConfig): CloudModel {
  if (!cfg.enterprise_cloud_model) {
    cfg.enterprise_cloud_model = { enabled: false, base_url: "", api_key: "", model: "", context_size: 32000, endpoints: [] };
  }
  return cfg.enterprise_cloud_model;
}

export function cloudShared(cfg: ServiceConfig): CloudShared {
  if (!cfg.cloud_shared) {
    cfg.cloud_shared = {
      timeout_seconds: 120,
      stream_timeout_seconds: 1800,
      log_debug: false,
      retry: { max: 2, backoff_ms: 200, max_total_attempts: 0, retry_on_429_switch_endpoint: false },
      circuit_breaker: { failure_threshold: 3, cooldown_seconds: 60 },
      rate_limit: { max_inferences_per_task: 20, max_tokens_per_task: 0 },
    };
  }
  const cs = cfg.cloud_shared;
  if (!cs.retry) cs.retry = { max: 2, backoff_ms: 200, max_total_attempts: 0, retry_on_429_switch_endpoint: false };
  if (cs.retry.max_total_attempts === undefined) cs.retry.max_total_attempts = 0;
  if (cs.retry.retry_on_429_switch_endpoint === undefined) cs.retry.retry_on_429_switch_endpoint = false;
  if (!cs.circuit_breaker) cs.circuit_breaker = { failure_threshold: 3, cooldown_seconds: 60 };
  if (!cs.rate_limit) cs.rate_limit = { max_inferences_per_task: 20, max_tokens_per_task: 0 };
  return cs;
}

export function routing(cfg: ServiceConfig): Routing {
  if (!cfg.routing) {
    cfg.routing = {
      enabled: true,
      prefer_local_for_simple: true,
      // V1 service_config.json (`schemas/service_config.schema.json`) does NOT
      // define the following four `enabled` keys; they were V2-only invented
      // toggles that have no effect on the wire. They are dropped from the
      // defaults below so the rendered config matches V1 byte-for-byte:
      //   • routing.fallback.enabled
      //   • routing.fallback.strategy
      //   • routing.agent_routing.enabled
      //   • routing.metrics.enabled
      // Wave 3 (RoutingTab realignment) MUST remove the four corresponding
      // ``v-model`` bindings in ServiceConfigRoutingTab.vue (currently lines
      // 64 / 73 / 179 / 356) and then drop the matching `enabled?: boolean`
      // optional declarations in types.ts (lines 59-60 / 71 / 93). Until
      // Wave 3 lands, the optionals in types.ts let those bindings keep
      // typechecking without re-introducing the spurious defaults here.
      fallback: {
        cloud_unavailable_to_local: true,
        clean_local_history_on_fallback: true,
        local_unavailable: { s0: "cloud_if_allowed", s1: "cloud_if_allowed", s2: "fail" },
        max_input_overflow_retries: 0,
        enterprise_cloud_unavailable: "public_cloud_if_allowed",
        public_cloud_unavailable: "enterprise_cloud_if_allowed",
      },
      agent_routing: { sub_agent_prefer_local: true, sub_agent_allow_cloud_on_c2: true, max_tool_call_retries: 10 },
      sticky_routing: { enabled: true, ttl_seconds: 1800, max_sessions: 1000 },
      incremental_check: { enabled: true, session_ttl_seconds: 3600, max_sessions: 1000, s2_always_full_check: true, detect_sensitive_reference: true, detect_history_tampering: true },
      s2_turn_cleaning: { enabled: true, log_details: true, allow_cloud_reroute_after_clean: true },
      metrics: { summary_every_n_requests: 100, summary_every_seconds: 0 },
      cache: { ttl_seconds: 60, max_entries: 256 },
    };
  }
  const r = cfg.routing;
  // Ensure nested objects exist with V1-parity defaults (no `enabled` keys
  // for fallback / agent_routing / metrics — see comment above).
  if (!r.fallback) r.fallback = { cloud_unavailable_to_local: true, clean_local_history_on_fallback: true, local_unavailable: { s0: "cloud_if_allowed", s1: "cloud_if_allowed", s2: "fail" }, max_input_overflow_retries: 0, enterprise_cloud_unavailable: "public_cloud_if_allowed", public_cloud_unavailable: "enterprise_cloud_if_allowed" };
  if (!r.fallback.local_unavailable) r.fallback.local_unavailable = { s0: "cloud_if_allowed", s1: "cloud_if_allowed", s2: "fail" };
  if (r.fallback.cloud_unavailable_to_local === undefined) r.fallback.cloud_unavailable_to_local = true;
  if (r.fallback.clean_local_history_on_fallback === undefined) r.fallback.clean_local_history_on_fallback = true;
  if (r.fallback.max_input_overflow_retries === undefined) r.fallback.max_input_overflow_retries = 0;
  if (!r.fallback.enterprise_cloud_unavailable) r.fallback.enterprise_cloud_unavailable = "public_cloud_if_allowed";
  if (!r.fallback.public_cloud_unavailable) r.fallback.public_cloud_unavailable = "enterprise_cloud_if_allowed";
  if (!r.agent_routing) r.agent_routing = { sub_agent_prefer_local: true, sub_agent_allow_cloud_on_c2: true, max_tool_call_retries: 10 };
  if (r.agent_routing.sub_agent_prefer_local === undefined) r.agent_routing.sub_agent_prefer_local = true;
  if (r.agent_routing.sub_agent_allow_cloud_on_c2 === undefined) r.agent_routing.sub_agent_allow_cloud_on_c2 = true;
  if (r.agent_routing.max_tool_call_retries === undefined) r.agent_routing.max_tool_call_retries = 10;
  if (!r.sticky_routing) r.sticky_routing = { enabled: true, ttl_seconds: 1800, max_sessions: 1000 };
  if (r.sticky_routing.max_sessions === undefined) r.sticky_routing.max_sessions = 1000;
  if (!r.incremental_check) r.incremental_check = { enabled: true, session_ttl_seconds: 3600, max_sessions: 1000, s2_always_full_check: true, detect_sensitive_reference: true, detect_history_tampering: true };
  if (r.incremental_check.session_ttl_seconds === undefined) r.incremental_check.session_ttl_seconds = 3600;
  if (r.incremental_check.max_sessions === undefined) r.incremental_check.max_sessions = 1000;
  if (r.incremental_check.s2_always_full_check === undefined) r.incremental_check.s2_always_full_check = true;
  if (r.incremental_check.detect_sensitive_reference === undefined) r.incremental_check.detect_sensitive_reference = true;
  if (r.incremental_check.detect_history_tampering === undefined) r.incremental_check.detect_history_tampering = true;
  if (!r.s2_turn_cleaning) r.s2_turn_cleaning = { enabled: true, log_details: true, allow_cloud_reroute_after_clean: true };
  if (r.s2_turn_cleaning.log_details === undefined) r.s2_turn_cleaning.log_details = true;
  if (r.s2_turn_cleaning.allow_cloud_reroute_after_clean === undefined) r.s2_turn_cleaning.allow_cloud_reroute_after_clean = true;
  if (!r.metrics) r.metrics = { summary_every_n_requests: 100, summary_every_seconds: 0 };
  if (r.metrics.summary_every_n_requests === undefined) r.metrics.summary_every_n_requests = 100;
  if (r.metrics.summary_every_seconds === undefined) r.metrics.summary_every_seconds = 0;
  if (!r.cache) r.cache = { ttl_seconds: 60, max_entries: 256 };
  return r;
}

export function sensitivityDetection(cfg: ServiceConfig): SensitivityDetection {
  const r = routing(cfg);
  if (!r.sensitivity_detection) {
    r.sensitivity_detection = {
      enabled: true, method: "rule_first", use_local_model_fallback: false, strict_s2_union: true,
      timeout_ms: 300000, model_input_max_chars: 2000, max_gen_tokens: 2048, debug_log_matches: true,
      keywords_dict_path: "./sensitive_keywords.json", keywords_reload_interval_seconds: 60,
      detection_rules: {
        enable_phone: true, level_phone: "S1", enable_email: true, level_email: "S1",
        enable_id_card: true, level_id_card: "S2", enable_bank_card: true, level_bank_card: "S2",
        enable_api_key: true, level_api_key: "S2", enable_private_key: true, level_private_key: "S2",
        enable_token: true, level_token: "S2", enable_password: true, level_password: "S1",
      },
      extended_rules: { enable_local_path: true, enable_internal_url: true, enable_device_id: true, enable_image_data: true },
    };
  }
  const sd = r.sensitivity_detection;
  if (!sd.detection_rules) sd.detection_rules = { enable_phone: true, level_phone: "S1", enable_email: true, level_email: "S1", enable_id_card: true, level_id_card: "S2", enable_bank_card: true, level_bank_card: "S2", enable_api_key: true, level_api_key: "S2", enable_private_key: true, level_private_key: "S2", enable_token: true, level_token: "S2", enable_password: true, level_password: "S1" };
  if (!sd.extended_rules) sd.extended_rules = { enable_local_path: true, enable_internal_url: true, enable_device_id: true, enable_image_data: true };
  return sd;
}

export function desensitization(cfg: ServiceConfig): Desensitization {
  const r = routing(cfg);
  if (!r.desensitization) {
    r.desensitization = {
      // V1 service_config wires `strategies` as a JSON array of strategy
      // identifiers (one or more), e.g. ["structured_placeholder"]. The
      // legacy V2 default stored a single string; align defaults to the
      // V1 array shape — the SecurityTab v-model still accepts the string
      // form during the 7-tab realignment thanks to the `string|string[]`
      // union in types.ts.
      enabled: true, strategies: ["structured_placeholder"], format_preserving_enabled: true,
      restore_response_enabled: true, restore_stream_enabled: true, iterative: true, max_rounds: 3,
      log_desensitization_details: false,
      entity_switches: {
        enable_phone: true, enable_email: true, enable_id_card: true, enable_bank_card: true,
        enable_api_key: true, enable_private_key: true, enable_token: true, enable_password: true,
        enable_internal_url: true, enable_local_path: true, enable_device_id: true, enable_image_data: true,
      },
    };
  }
  const d = r.desensitization;
  if (!d.entity_switches) d.entity_switches = { enable_phone: true, enable_email: true, enable_id_card: true, enable_bank_card: true, enable_api_key: true, enable_private_key: true, enable_token: true, enable_password: true, enable_internal_url: true, enable_local_path: true, enable_device_id: true, enable_image_data: true };
  return d;
}

export function complexity(cfg: ServiceConfig): ComplexityAssessment {
  const r = routing(cfg);
  if (!r.complexity) {
    r.complexity = { method: "heuristic_first", use_local_model_fallback: false, timeout_ms: 300000, model_input_max_chars: 2000, thresholds: { tool_calls: 5 } };
  }
  if (!r.complexity.thresholds) r.complexity.thresholds = { tool_calls: 5 };
  return r.complexity;
}

export function promptOpt(cfg: ServiceConfig): PromptOptimization {
  if (!cfg.prompt_optimization) {
    cfg.prompt_optimization = {
      allowed_tools: [], skill_catalog_format: "structured", enable_skill_auto_correction: true,
      enable_tool_whitelist: true, tool_call_temperature: 0.1, spawn_guard: { enabled: true },
      max_messages_limit: 16, recent_window: 6, output_reserve_ratio: 0.20,
      old_compress_len: 100, recent_compress_len: 300, tool_compress_len: 300,
      min_compress_threshold: 60, tool_min_length: 100,
      emergency_truncation: { enabled: true, max_truncation_ratio: 0.40, safety_margin_tokens: 30 },
      long_text_summarization: {
        enabled: false, trigger_ratio: 0.5, chunk_ratio: 0.45, max_chunks: 4,
        summarize_user_messages: true, summarize_tool_responses: true, verbose_logging: false,
        cache: { enabled: true, max_entries: 500, max_memory_mb: 50, ttl_minutes: 60 },
      },
    };
  }
  const po = cfg.prompt_optimization;
  if (!po.spawn_guard) po.spawn_guard = { enabled: true };
  if (po.skill_catalog_format === undefined) po.skill_catalog_format = "structured";
  if (po.enable_skill_auto_correction === undefined) po.enable_skill_auto_correction = true;
  if (po.enable_tool_whitelist === undefined) po.enable_tool_whitelist = true;
  if (po.tool_call_temperature === undefined) po.tool_call_temperature = 0.1;
  if (po.max_messages_limit === undefined) po.max_messages_limit = 16;
  if (po.recent_window === undefined) po.recent_window = 6;
  if (po.output_reserve_ratio === undefined) po.output_reserve_ratio = 0.20;
  if (po.old_compress_len === undefined) po.old_compress_len = 100;
  if (po.recent_compress_len === undefined) po.recent_compress_len = 300;
  if (po.tool_compress_len === undefined) po.tool_compress_len = 300;
  if (po.min_compress_threshold === undefined) po.min_compress_threshold = 60;
  if (po.tool_min_length === undefined) po.tool_min_length = 100;
  if (!po.emergency_truncation) po.emergency_truncation = { enabled: true, max_truncation_ratio: 0.40, safety_margin_tokens: 30 };
  if (!po.long_text_summarization) po.long_text_summarization = { enabled: false, trigger_ratio: 0.5, chunk_ratio: 0.45, max_chunks: 4, summarize_user_messages: true, summarize_tool_responses: true, verbose_logging: false, cache: { enabled: true, max_entries: 500, max_memory_mb: 50, ttl_minutes: 60 } };
  if (!po.long_text_summarization.cache) po.long_text_summarization.cache = { enabled: true, max_entries: 500, max_memory_mb: 50, ttl_minutes: 60 };
  return po;
}

export function systemPromptSections(cfg: ServiceConfig): NonNullable<NonNullable<PromptOptimization["system_prompts"]>["sections_enabled"]> {
  const po = promptOpt(cfg);
  if (!po.system_prompts) po.system_prompts = {};
  if (!po.system_prompts.sections_enabled) {
    po.system_prompts.sections_enabled = { critical_rule: true, tools_intro: true, catalog_structured_intro: true };
  }
  const s = po.system_prompts.sections_enabled;
  if (s.critical_rule === undefined) s.critical_rule = true;
  if (s.tools_intro === undefined) s.tools_intro = true;
  if (s.catalog_structured_intro === undefined) s.catalog_structured_intro = true;
  return s;
}

export function fewShotExamples(cfg: ServiceConfig): NonNullable<NonNullable<PromptOptimization["system_prompts"]>["few_shot_examples_enabled"]> {
  const po = promptOpt(cfg);
  if (!po.system_prompts) po.system_prompts = {};
  if (!po.system_prompts.few_shot_examples_enabled) {
    po.system_prompts.few_shot_examples_enabled = { enabled: true, skill_correct_call: true, no_skill_needed: true, max_skill_examples: 1 };
  }
  const f = po.system_prompts.few_shot_examples_enabled;
  if (f.enabled === undefined) f.enabled = true;
  if (f.skill_correct_call === undefined) f.skill_correct_call = true;
  if (f.no_skill_needed === undefined) f.no_skill_needed = true;
  if (f.max_skill_examples === undefined) f.max_skill_examples = 1;
  return f;
}

export function cloudUploadPolicy(cfg: ServiceConfig): NonNullable<CloudModel["upload_policy"]> {
  const cm = cloudModel(cfg);
  if (!cm.upload_policy) {
    cm.upload_policy = { enable_sensitivity_check: true, enable_desensitization: true };
  }
  return cm.upload_policy;
}

export function debugCfg(cfg: ServiceConfig): ServiceDebug {
  if (!cfg.debug) {
    cfg.debug = {
      status_update_content_visible: false,
      log_rule_matches: false,
      log_inference_stream: false,
    };
  }
  return cfg.debug;
}

// ── String <-> structured conversion helpers ──────────────────────────────

export function keywordsToText(kw: string[] | undefined): string {
  return Array.isArray(kw) ? kw.join(", ") : "";
}

export function textToKeywords(text: string): string[] {
  return text.split(",").map((s) => s.trim()).filter(Boolean);
}

export function allowedToolsToText(tools: string[]): string {
  return Array.isArray(tools) ? tools.join(", ") : "";
}

export function textToAllowedTools(text: string): string[] {
  return text.split(",").map((s) => s.trim()).filter(Boolean);
}
