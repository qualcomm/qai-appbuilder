//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef MODEL_CONFIG_H
#define MODEL_CONFIG_H

#include "def.h"
#include <nlohmann/json.hpp>
#include <limits>
#include <mutex>
#include <memory>

using json = nlohmann::ordered_json;

class ContextBase;

struct RoutingConfig {
    bool enabled = false;
    std::string policy_id = "default_v1";
    bool prefer_local_for_simple = true;

    struct SensitivityDetectionConfig {
        bool enabled = true;
        std::string method = "rule_first";
        bool use_local_model_fallback = true;
        bool strict_s2_union = true;
        int timeout_ms = 300000;
        int model_input_max_chars = 2000;
        int max_gen_tokens = 2048;

        std::string system_prompt;

        std::unordered_map<std::string, std::string> rule_level_overrides;

        std::string keywords_dict_path = "";
        int keywords_reload_interval_seconds = 60;

        struct ExtendedRulesConfig {
            bool enable_local_path   = true;
            bool enable_internal_url = true;
            bool enable_device_id    = true;
            bool enable_image_data   = true;
        } extended_rules;

        struct DetectionRulesConfig {
            bool enable_phone       = true;
            std::string level_phone       = "S1";
            bool enable_email       = true;
            std::string level_email       = "S1";
            bool enable_id_card     = true;
            std::string level_id_card     = "S2";
            bool enable_bank_card   = true;
            std::string level_bank_card   = "S2";
            bool enable_api_key     = true;
            std::string level_api_key     = "S2";
            bool enable_private_key = true;
            std::string level_private_key = "S2";
            bool enable_token       = true;
            std::string level_token       = "S2";
            bool enable_password    = true;
            std::string level_password    = "S1";
        } detection_rules;

        bool debug_log_matches = false;
    } sensitivity_detection;

    struct DesensitizationConfig {
        bool enabled = true;
        std::vector<std::string> strategies = {"mask", "placeholder", "delete", "summarize"};
        std::string placeholder_style = "<{type}_{index}>";
        bool iterative = true;
        int max_rounds = 3;

        int summarize_timeout_ms = 300000;

        std::string system_prompt;

        bool format_preserving_enabled = false;

        bool restore_response_enabled = true;

        bool restore_stream_enabled = true;

        bool log_desensitization_details = false;

        struct EntitySwitchesConfig {
            bool enable_phone        = true;
            bool enable_email        = true;
            bool enable_id_card      = true;
            bool enable_bank_card    = true;
            bool enable_api_key      = true;
            bool enable_private_key  = true;
            bool enable_token        = true;
            bool enable_password     = true;
            bool enable_internal_url = true;
            bool enable_local_path   = true;
            bool enable_device_id    = true;
            bool enable_image_data   = true;
        } entity_switches;
    } desensitization;

    struct ComplexityConfig {
        std::string method = "heuristic_first";
        bool use_local_model_fallback = true;
        int timeout_ms = 300000;

        int model_input_max_chars = 2000;

        std::string system_prompt;
        struct Thresholds {
            int tool_calls = 3;
        } thresholds;

        std::vector<std::string> keywords_c1;
        std::vector<std::string> keywords_c2;
    } complexity;

    bool enterprise_cloud_require_desensitize = false;

    struct FallbackConfig {
        bool cloud_unavailable_to_local = true;
        std::string local_unavailable_s0 = "cloud_if_allowed";
        std::string local_unavailable_s1 = "cloud_if_allowed";
        std::string local_unavailable_s2 = "fail";

        bool clean_local_history_on_fallback = true;

        int max_input_overflow_retries = 3;

        std::string enterprise_cloud_unavailable = "public_cloud_if_allowed";

        std::string public_cloud_unavailable = "enterprise_cloud_if_allowed";
    } fallback;

    struct CacheConfig {
        int ttl_seconds = 60;
        int max_entries = 256;
    } cache;

    struct AgentRoutingConfig {
        bool sub_agent_allow_cloud_on_c2 = true;
    } agent_routing;

    struct StickyRoutingConfig {
        bool enabled = false;
        int ttl_seconds = 1800;
        int max_sessions = 1000;
    } sticky_routing;

    struct IncrementalCheckConfig {
        bool enabled = false;
        int session_ttl_seconds = 3600;
        int max_sessions = 1000;
        bool s2_always_full_check = true;
        bool detect_sensitive_reference = true;
        bool detect_history_tampering = true;
    } incremental_check;

    struct S2TurnCleaningConfig {
        bool enabled = true;
        bool log_details = true;

        bool allow_cloud_reroute_after_clean = false;
    } s2_turn_cleaning;

    struct MetricsConfig {
        int summary_every_n_requests = 100;
        int summary_every_seconds = 0;
        int latency_sample_size = 1000;
        int fail_reason_topn = 5;
    } metrics;
};

struct LocalModelConfig {
    bool enabled = true;
};

struct ToolCallRepairConfig {
    bool enabled = true;

    struct InternalRetryConfig {

        int max_attempts = 1;

        std::vector<std::string> skip_reasons = {"truncated"};
    } internal_retry;

    struct CircuitBreakerConfig {

        int consecutive_layer3_threshold = 3;
    } circuit_breaker;

    struct RedundantToolCallConfig {
        bool enabled = true;
        int repeat_threshold = 2;
        int consecutive_failure_threshold = 3;

        int non_contiguous_repeat_threshold = 2;

        int internal_retry_threshold = 2;
        int hard_block_threshold = 3;

        int hard_block_stop_after = 2;

        bool soft_retry_enabled = false;

        int max_signatures_per_entry = 256;

        int note_min_interval_turns = 4;

        int overlap_reread_threshold = 2;
    } redundant_call_guard;
};

struct WindowsShellHintConfig {
    bool enabled = true;
    std::vector<std::string> tool_keywords = {"cmd", "powershell", "shell", "exec", "bash", "terminal", "command"};
    std::string hint_text =
        "You are running on Windows. Shell/command-execution tools may route your command to either "
        "cmd.exe or PowerShell. cmd.exe: chain with && or ||, read variables as %VAR%, escape quotes as \"\". "
        "PowerShell: chain with ;, read variables as $env:VAR, nested quotes need escaping or single-quoting. "
        "If a command fails with a shell syntax error, retry using the other shell's syntax. "
        "Your command already runs inside a shell: write it directly in PowerShell, never wrapping it in "
        "another shell nor mixing cmd syntax in: Write-Output (\"os=\"+$env:OS). Nested quotes must differ from "
        "the outer: python -c \"import platform;print('m='+platform.machine())\". Needing two levels means write "
        "the script to a file and run it. List files with Get-ChildItem -Name or -Recurse -Filter *.py, not "
        "dir /b or /s /b. wmic is gone; use cmdlets.";
};

struct AdaptiveThinkingConfig {
    bool enabled = true;
    int time_budget_s = 15;
    int min_tokens = 320;
    int max_per_signature = 2;
    double max_think_turn_share = 0.4;
    int error_repeat_window = 4;
    int near_repeat_window = 5;
    int max_tokens = 0;
    int ctx_reserve_tokens = 2048;
    int goal_reminder_chars = 0;

    int max_consecutive_block = 6;

    int reserved_failure_think_turns = 3;
};

struct GenerationRepeatGuardConfig {
    bool enabled = true;
    int max_period_tokens = 48;
};

struct CloudModelConfig {
    bool enabled = false;
    std::string base_url;
    std::string api_key;
    std::string model;
    int timeout_seconds = 30;

    int stream_timeout_seconds = 0;

    int context_size = 0;
    static constexpr int DEFAULT_CLOUD_CONTEXT_SIZE = 32768;

    struct UploadPolicyConfig {

        bool enable_sensitivity_check = true;

        bool enable_desensitization = true;
    } upload_policy;

    struct RetryConfig {
        int max = 2;
        int backoff_ms = 200;

        int max_total_attempts = 0;

        bool retry_on_429_switch_endpoint = false;
    } retry;

    struct CircuitBreakerConfig {
        int failure_threshold = 3;
        int cooldown_seconds = 60;
    } circuit_breaker;

    struct RateLimitConfig {

        int max_inferences_per_task = 20;

        int max_tokens_per_task = 0;
    } rate_limit;

    struct Endpoint {
        std::string name;
        std::string base_url;
        std::string model;
    };
    std::vector<Endpoint> endpoints;

    bool log_debug = false;
};

struct EnterpriseCloudModelConfig {
    bool enabled = false;
    std::string base_url;
    std::string api_key;
    std::string model;
    int timeout_seconds = 60;

    int stream_timeout_seconds = 0;

    int context_size = 0;
    static constexpr int DEFAULT_ENTERPRISE_CLOUD_CONTEXT_SIZE = 16384;

    struct RetryConfig {
        int max = 2;
        int backoff_ms = 200;
        int max_total_attempts = 0;
        bool retry_on_429_switch_endpoint = false;
    } retry;

    struct CircuitBreakerConfig {
        int failure_threshold = 3;
        int cooldown_seconds = 60;
    } circuit_breaker;

    struct RateLimitConfig {
        int max_inferences_per_task = 20;
        int max_tokens_per_task = 0;
    } rate_limit;

    struct Endpoint {
        std::string name;
        std::string base_url;
        std::string model;
    };
    std::vector<Endpoint> endpoints;

    bool log_debug = false;
};

struct SystemContextSection {
    std::string title;
    std::vector<std::string> lines;
    bool enabled = true;
};

struct SystemContextConfig {
    std::vector<SystemContextSection> sections;
};

struct PromptSectionRule {
    std::string title_contains;
    int heading_level = 0;
    bool include = true;
};

struct PromptSectionsConfig {
    std::vector<PromptSectionRule> rules;
    std::string default_action = "exclude";
    int max_section_tokens = 0;
    bool enabled = false;
};

struct SystemPromptsConfig {

    struct SectionsEnabled {
        bool identity_intro           = true;
        bool skill_rule               = true;
        bool tools_intro              = true;
        bool catalog_structured_intro = true;
        bool verification_first_rule  = true;
    } sections_enabled;

    struct FewShotExamplesEnabled {
        bool enabled            = true;
        bool skill_correct_call = true;
        bool no_skill_needed    = true;

        int  max_skill_examples = 2;
    } few_shot_examples_enabled;

    std::string identity_intro;
    std::string skill_rule;
    std::string tools_intro;
    std::string catalog_structured_intro;

    std::string few_shot_header    = "## Examples\n\n";

    std::string few_shot_skill_title_template       = "**Example {idx} - Skill Match**\n";
    std::string few_shot_default_user_query_prefix  = "Please use the ";
    std::string few_shot_default_user_query_suffix  = " skill";
    std::string few_shot_user_label                 = "User: ";
    std::string few_shot_response_label             = "Response: ";
    std::string few_shot_correct_call_label         = "Tool(correct skill call): ";
    std::string few_shot_no_skill_title_template    = "**Example {idx} - List Skills (answer from catalog)**\n";
    std::string few_shot_no_skill_user_input        = "What skills do you have? / 有哪些skills?";
    std::string few_shot_no_skill_response          = "I have the following skills: [list from catalog above]. No tool call needed.";
};

struct RequestTraceConfig {
    bool enabled = false;
    std::string path = "tmp/request_trace.jsonl";
};

struct PromptOptimizationConfig {

    std::string skill_catalog_format = "structured";
    bool enable_skill_auto_correction = true;
    SystemPromptsConfig system_prompts;

    struct RelevanceFilterConfig {
        bool enabled = true;
        size_t name_token_weight = 3;
        size_t description_keyword_weight = 1;

        bool zero_hit_keep_all = true;

        bool cjk_bigram = true;

        bool intent_aliases_enabled = true;

        size_t tag_weight = 2;
        bool anchor_first_user_message = true;
        double keep_all_tools_ratio = 0.15;
        bool protect_core_capability_tools = true;
        std::vector<std::string> core_capability_keywords = {
            "read", "write", "edit", "create", "save", "patch", "append", "delete",
            "list", "glob", "grep", "find", "search", "dir", "tree",
            "exec", "run", "shell", "command", "terminal", "bash", "powershell", "cmd"};
    } relevance_filter;

    struct SkillDisclosureConfig {
        bool enabled = true;
        size_t l2_top_k = 2;
        size_t l1_top_k = 6;
        size_t l1_summary_max_chars = 240;
        size_t l0_summary_max_chars = 80;

        bool tie_aware_l2 = false;
    } skill_disclosure;

    float output_reserve_ratio = 0.20f;

    size_t max_messages_limit = 16;

    double token_pressure_trigger_ratio = 0.75;
    size_t recent_window = 6;

    size_t old_compress_len = 300;
    size_t recent_compress_len = 600;
    size_t tool_compress_len = 400;

    size_t min_compress_threshold = 10;
    size_t tool_min_length = 300;

    struct EmergencyTruncationConfig {
        bool enabled = true;
        float max_truncation_ratio = 0.95f;

        int safety_margin_tokens = 30;

        bool target_largest_tool_messages = true;
    } emergency_truncation;

    struct FidelityConfig {
        std::string budget_unit = "tokens";

        bool   preserve_tail = true;
        double tail_ratio = 0.30;
        bool   extract_high_signal = true;
        bool   drop_placeholder = true;
        size_t json_head_items = 3;
        size_t json_tail_items = 1;
        size_t max_token_probe_per_message = 8;

        double cjk_bytes_per_token = 3.0;
        double ascii_bytes_per_token = 4.0;
    } fidelity;

    struct BudgetPartitionConfig {
        bool enabled = true;

        double skills_floor_ratio = 0.15;

        double tools_ratio = 0.35;
    } budget_partition;

    PromptSectionsConfig prompt_sections;

    PromptSectionsConfig subagent_prompt_sections;

    SystemContextConfig system_context;

    struct SpawnGuardConfig {
        bool enabled = true;

        std::string header = "[SPAWN_GUARD] Child session accepted: {child_key}";

        std::string body =
            "STOP. Do NOT call sessions_spawn again.\n"
            "The child is running asynchronously. Its result will arrive as a new user message.\n"
            "Your ONLY valid next action is to output plain text (no tool calls) to acknowledge the spawn.\n"
            "Wait silently. Do not repeat the spawn.";
    } spawn_guard;

    struct LongTextSummaryCacheConfig {
        bool enabled = true;
        size_t max_entries = 500;
        size_t max_memory_mb = 50;
        int ttl_minutes = 60;
    };

    struct LongTextSummarizationConfig {
        bool enabled = false;
        double trigger_ratio = 0.9;
        double chunk_ratio = 0.45;
        bool summarize_user_messages = true;
        bool summarize_tool_responses = true;
        int max_chunks = 4;
        bool verbose_logging = false;

        std::string map_instruction =
            "Summarize the following content concisely but completely. Preserve facts, file paths, "
            "code snippets, errors, numbers, and technical details.";

        std::string reduce_instruction =
            "Merge the following partial summaries into one coherent summary. Preserve all important "
            "technical information and remove duplication.";

        LongTextSummaryCacheConfig cache;
    } long_text_summarization;

    struct TaskMemoStoreConfig {
        size_t max_entries = 200;
        size_t max_memory_mb = 20;
    };

    struct TaskMemoConfig {
        bool enabled = false;
        bool model_layer_enabled = true;
        double token_budget_ratio = 0.15;
        size_t min_dropped_for_trigger = 1;

        size_t render_throttle_min_new_dropped = 4;
        size_t long_tool_chain_threshold = 4;
        double low_confidence_threshold = 0.5;
        int rule_layer_preview_chars = 160;
        double memo_reserve_ratio = 0.3;

        size_t goal_scan_window = 3;
        size_t min_goal_signal_chars = 20;
        size_t goal_anchor_preview_chars = 300;

        int failure_streak_warn_threshold = 2;

        struct PageDirectoryConfig {
            size_t max_entries = 20;
        } page_directory;

        TaskMemoStoreConfig store;
    } task_memo;
};

using SkillMappings = std::unordered_map<std::string, std::string>;

struct SkillInfo {
    std::string name;
    std::string path;
    std::string use_for;
};

using RuntimeSkillMappings = std::unordered_map<std::string, SkillInfo>;

class IModelConfig
{
public:
    virtual ~IModelConfig() = default;

    const std::string &get_config_path() const
    {
        return config_file_;
    }

    const json &get_prompt_template() const
    {
        return prompt_;
    }

    const std::string &get_model_path() const{return model_path_;}

    const std::string &get_model_name() const
    {
        return model_name_;
    }

    bool getisOutputAllText() const
    {
        return outputAllText;
    }

    bool getenableThinking() const
    {
        return enableThinking;
    }

    int getenablePromptDebug() const
    {
        return enablePromptDebug;
    }

    int getnumResponse() const
    {
        return num_response_;
    }

    const QNNEmbedding &get_qnn_embedding() const
    {
        return qnn_embedding_;
    }

    PromptType get_prompt_type() const
    {
        return prompt_type_;
    }

    bool is_thinking_model() const
    {
        return thinking_model_;
    }

    std::weak_ptr<ContextBase> get_genie_model_handle() {return genieModelHandle;}

    virtual std::weak_ptr<ContextBase> GetDefaultModelHandle() const
    {
        return genieModelHandle;
    }

    virtual bool IsLocalModelAvailable() const
    {
        return genieModelHandle != nullptr;
    }

    virtual const class ModelInstanceConfig* GetDefaultInstanceConfig() const
    {
        return nullptr;
    }

    const RoutingConfig &GetRoutingConfig() const
    {
        return routing_config_;
    }

    const CloudModelConfig &GetCloudModelConfig() const
    {
        return cloud_model_config_;
    }

    const EnterpriseCloudModelConfig &GetEnterpriseCloudModelConfig() const
    {
        return enterprise_cloud_model_config_;
    }

    const LocalModelConfig &GetLocalModelConfig() const
    {
        return local_model_config_;
    }

    const ToolCallRepairConfig &GetToolCallRepairConfig() const
    {
        return tool_call_repair_config_;
    }

    const WindowsShellHintConfig &GetWindowsShellHintConfig() const
    {
        return windows_shell_hint_config_;
    }

    const AdaptiveThinkingConfig &GetAdaptiveThinkingConfig() const
    {
        return adaptive_thinking_config_;
    }

    const GenerationRepeatGuardConfig &GetGenerationRepeatGuardConfig() const
    {
        return generation_repeat_guard_config_;
    }

    const PromptOptimizationConfig& GetPromptOptimizationConfig() const
    {
        return prompt_optimization_config_;
    }

    const RequestTraceConfig& GetRequestTraceConfig() const
    {
        return request_trace_config_;
    }

    const PromptSectionsConfig& GetPromptSectionsConfig() const
    {
        return prompt_optimization_config_.prompt_sections;
    }

    const PromptSectionsConfig& GetSubagentPromptSectionsConfig() const
    {
        return prompt_optimization_config_.subagent_prompt_sections;
    }

    const SystemContextConfig& GetSystemContextConfig() const
    {
        return prompt_optimization_config_.system_context;
    }

    void SetRuntimeSkillMappings(const SkillMappings& mappings)
    {
        std::lock_guard<std::mutex> lock(*runtime_skill_mappings_mutex_);
        runtime_skill_mappings_ = mappings;
    }

    const SkillMappings GetRuntimeSkillMappings() const
    {
        std::lock_guard<std::mutex> lock(*runtime_skill_mappings_mutex_);
        return runtime_skill_mappings_;
    }

    std::shared_ptr<ContextBase> genieModelHandle{};
    std::string model_root_;
    std::string model_path_;
    std::string model_name_;
    std::string known_model_path_;
    std::vector<std::string> config_model_name_list_;
    mutable std::vector<std::string> model_list_;
    std::shared_ptr<std::mutex> model_list_mutex_{std::make_shared<std::mutex>()};

    json prompt_{json::object()};
    bool thinking_model_{false};
    PromptType prompt_type_{};
    ModelFormat model_format_{};
    int log_level_{-1};

    std::string config_file_;
    std::string loraAdapter = "default_adapter";
    bool outputAllText = false;
    bool enableThinking = false;
    int enablePromptDebug = 0;
    int num_response_ = 30;
    int minOutputNum = 512;
    float loraAlpha = 0.5;
    QNNEmbedding qnn_embedding_;

    RoutingConfig routing_config_;
    CloudModelConfig cloud_model_config_;
    EnterpriseCloudModelConfig enterprise_cloud_model_config_;
    LocalModelConfig local_model_config_;
    ToolCallRepairConfig tool_call_repair_config_;
    WindowsShellHintConfig windows_shell_hint_config_;
    AdaptiveThinkingConfig adaptive_thinking_config_;
    GenerationRepeatGuardConfig generation_repeat_guard_config_;

    PromptOptimizationConfig prompt_optimization_config_;

    RequestTraceConfig request_trace_config_;

    SkillMappings runtime_skill_mappings_;
    std::shared_ptr<std::mutex> runtime_skill_mappings_mutex_{std::make_shared<std::mutex>()};

    void UpdateModeList() const;

    friend class Config;
};

#endif
