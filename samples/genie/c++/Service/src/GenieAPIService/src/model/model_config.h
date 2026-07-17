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
#include <mutex>
#include <memory>

using json = nlohmann::ordered_json;

class ContextBase;

// ============================================================
// 路由配置结构（对应 service_config.json 中的 "routing" 节）
// ============================================================
struct RoutingConfig {
    bool enabled = false;
    std::string policy_id = "default_v1";
    bool prefer_local_for_simple = true;

    struct SensitivityDetectionConfig {
        bool enabled = true;  // 总开关：false=跳过所有敏感检测，所有数据直接上云（默认 true）
        std::string method = "rule_first";
        bool use_local_model_fallback = true;
        bool strict_s2_union = true;
        int timeout_ms = 300000;  // 本地模型辅助判断超时（毫秒），默认 5 分钟
        int model_input_max_chars = 2000; // 辅助判断输入截断长度（字符数）
        int max_gen_tokens = 2048; // 安全检查最大生成 token 数，默认 2048（思考类模型需要更多 token）
        // 本地模型辅助安全检测时使用的系统提示词（从 service_config.json 加载，空字符串时使用内置默认值）
        std::string system_prompt;
        // rule_id → 默认敏感等级覆盖（可审计），对应需求文档 §6.5
        // key: rule_id, value: "S1" or "S2"
        std::unordered_map<std::string, std::string> rule_level_overrides;
        // 关键词词典配置
        std::string keywords_dict_path = "";
        int keywords_reload_interval_seconds = 60;
        // [扩展规则] 各类扩展检测规则的独立开关
        // 每类规则可通过 service_config.json 中的 sensitivity_detection.extended_rules 独立开启/关闭
        struct ExtendedRulesConfig {
            bool enable_local_path   = true;  // 本地文件路径检测 (R_PATH_UNIX, R_PATH_WIN)
            bool enable_internal_url = true;  // 内网地址检测 (R_INTERNAL_IP, R_INTERNAL_DOMAIN)
            bool enable_device_id    = true;  // 设备标识检测 (R_MAC_ADDR, R_IMEI)
            bool enable_image_data   = true;  // 图片数据基础保护 (R_BASE64_IMAGE)
        } extended_rules;
        // [基础规则] 各类基础检测规则的独立开关
        // 每类规则可通过 service_config.json 中的 sensitivity_detection.detection_rules 独立开启/关闭
        // 注意：关闭 S2 级别规则（id_card/bank_card/api_key/private_key/token）会降低安全保护等级
        struct DetectionRulesConfig {
            bool enable_phone       = true;  // 手机号检测 (R_PHONE_CN)
            std::string level_phone       = "S1";  // 可配置敏感等级（S0/S1/S2），默认 S1
            bool enable_email       = true;  // 邮箱检测 (R_EMAIL)
            std::string level_email       = "S1";  // 可配置敏感等级，默认 S1
            bool enable_id_card     = true;  // 身份证检测 (R_ID_CARD)
            std::string level_id_card     = "S2";  // 可配置敏感等级，默认 S2
            bool enable_bank_card   = true;  // 银行卡检测 (R_BANK_CARD_KW + R_BANK_CARD_FMT)
            std::string level_bank_card   = "S2";  // 可配置敏感等级，默认 S2
            bool enable_api_key     = true;  // API Key 检测 (R_API_KEY，sk- 开头)
            std::string level_api_key     = "S2";  // 可配置敏感等级，默认 S2
            bool enable_private_key = true;  // PEM 私钥块检测 (R_PRIVATE_KEY)
            std::string level_private_key = "S2";  // 可配置敏感等级，默认 S2
            bool enable_token       = true;  // Bearer Token 检测 (R_TOKEN)
            std::string level_token       = "S2";  // 可配置敏感等级，默认 S2
            bool enable_password    = true;  // 密码关键词检测 (R_PASSWORD_KW，password=xxx 模式)
            std::string level_password    = "S1";  // 可配置敏感等级，默认 S1
        } detection_rules;
        // [调试] 是否在规则命中时打印匹配规则和原始文本（默认关闭，仅用于问题排查）
        bool debug_log_matches = false;
    } sensitivity_detection;

    struct DesensitizationConfig {
        bool enabled = true;
        std::vector<std::string> strategies = {"mask", "placeholder", "delete", "summarize"};
        std::string placeholder_style = "<{type}_{index}>";
        bool iterative = true; // 是否允许迭代脱敏
        int max_rounds = 3;     // 最大迭代轮数
        // 摘要化脱敏超时（毫秒），对应 §13.4.2
        int summarize_timeout_ms = 300000;  // 摘要化脱敏超时（毫秒），默认 5 分钟
        // 本地模型摘要化脱敏时使用的系统提示词（从 service_config.json 加载，空字符串时使用内置默认值）
        std::string system_prompt;
        // 格式保留脱敏开关（工具调用场景）
        bool format_preserving_enabled = false;  // 默认关闭,需手动开启
        // 响应还原开关（格式保留脱敏场景）
        bool restore_response_enabled = true;  // 默认开启，将 Mock Data 还原为原始数据
        // 流式响应还原开关（格式保留脱敏场景）
        bool restore_stream_enabled = true;  // 默认开启，对 SSE 流式响应执行字段级 JSON 还原
        // 脱敏过程详细日志开关（包含原始敏感信息，仅用于开发调试）
        bool log_desensitization_details = false;  // 默认关闭，生产环境推荐保持关闭
        // 各实体类型脱敏开关（false=跳过该类型的脱敏处理，不影响其他类型）
        // 可通过 service_config.json 中的 desensitization.entity_switches 独立控制
        struct EntitySwitchesConfig {
            bool enable_phone        = true;  // 手机号脱敏
            bool enable_email        = true;  // 邮箱脱敏
            bool enable_id_card      = true;  // 身份证脱敏
            bool enable_bank_card    = true;  // 银行卡脱敏
            bool enable_api_key      = true;  // API Key 脱敏
            bool enable_private_key  = true;  // 私钥脱敏
            bool enable_token        = true;  // Bearer Token 脱敏
            bool enable_password     = true;  // 密码关键词脱敏
            bool enable_internal_url = true;  // 内网 IP/域名脱敏 (R_INTERNAL_IP, R_INTERNAL_DOMAIN)
            bool enable_local_path   = true;  // 本地路径脱敏 (R_PATH_WIN, R_PATH_UNIX)
            bool enable_device_id    = true;  // 设备标识脱敏 (R_MAC_ADDR, R_IMEI)
            bool enable_image_data   = true;  // 图片数据脱敏 (R_BASE64_IMAGE)
        } entity_switches;
    } desensitization;

    struct ComplexityConfig {
        std::string method = "heuristic_first";
        bool use_local_model_fallback = true;
        int timeout_ms = 300000;  // 本地模型辅助判断超时（毫秒），默认 5 分钟
        // 辅助判断输入截断长度（字符数），与 SensitivityDetectionConfig 对齐
        int model_input_max_chars = 2000;
        // 本地模型辅助复杂度评估时使用的系统提示词（从 service_config.json 加载，空字符串时使用内置默认值）
        std::string system_prompt;
        struct Thresholds {
            int tool_calls = 3;
        } thresholds;
        // 复杂度关键词列表（可通过 service_config.json 配置，空列表时使用内置默认值）
        // 注意：关键词匹配时自动转换为小写，配置中无需区分大小写
        std::vector<std::string> keywords_c1;  // C1 关键词：命中后升级为 C1
        std::vector<std::string> keywords_c2;  // C2 关键词：命中后直接升级为 C2
    } complexity;

    // 企业内网云是否要求对 S1 数据脱敏后再上传
    // true  = S1 数据发往企业云前必须脱敏（保守模式，适合对数据合规有要求的企业）
    // false = 企业云视为可信边界，S1 数据无需脱敏直接发送（默认，适合物理隔离的内网部署）
    bool enterprise_cloud_require_desensitize = false;

    struct FallbackConfig {
        bool cloud_unavailable_to_local = true;
        std::string local_unavailable_s0 = "cloud_if_allowed";
        std::string local_unavailable_s1 = "cloud_if_allowed";
        std::string local_unavailable_s2 = "fail";
        // 事后路由回退时清理本地历史记录（默认开启）
        // 触发 HandleLocalOutputOverflow 时，将 messages 中本地模型产生的
        // 中间 tool_calls/tool 消息剔除，只保留原始对话历史再发给云端
        bool clean_local_history_on_fallback = true;
        // 本地输入溢出时云端可恢复错误（503）的最大重试次数
        // 超出后将 503 升级为 422（永久失败），阻止客户端无限重试
        // 0 = 不限制（不推荐）；默认 3
        int max_input_overflow_retries = 3;
        // 企业云不可用时的 fallback 策略
        // "public_cloud_if_allowed" = 降级到公有云（默认）
        // "local_if_allowed"        = 降级到本地
        // "fail"                    = 直接返回 503
        std::string enterprise_cloud_unavailable = "public_cloud_if_allowed";
        // 公有云不可用时的 fallback 策略
        // "enterprise_cloud_if_allowed" = 降级到企业云（默认）
        // "local_if_allowed"            = 降级到本地
        // "fail"                        = 直接返回 503
        std::string public_cloud_unavailable = "enterprise_cloud_if_allowed";
    } fallback;

    struct CacheConfig {
        int ttl_seconds = 60;   // 缓存生存时间（秒）
        int max_entries = 256;  // LRU 缓存最大条目数
    } cache;

    struct AgentRoutingConfig {
        bool sub_agent_prefer_local = true;      // 子 agent 默认优先本地
        bool sub_agent_allow_cloud_on_c2 = true; // 子 agent 在 C2 时允许上云
        int max_tool_call_retries = 10;           // 当次请求最大连续工具调用重试次数
    } agent_routing;

    // 会话级路由锁定配置
    // 一旦某个 session 被路由到 CLOUD，该 session 后续所有请求均走 CLOUD，
    // 直到任务完成（客户端开启新 session）或 TTL 超时
    struct StickyRoutingConfig {
        bool enabled = false;       // 默认关闭
        int ttl_seconds = 1800;     // session 锁定超时（默认 30 分钟）
        int max_sessions = 1000;    // 最大锁定 session 数（防内存泄漏）
    } sticky_routing;

    // [增量检查优化] 安全检查增量模式配置
    // 在多轮对话中只检查新增消息，而不是完整历史，以降低 CPU 和推理延迟
    struct IncrementalCheckConfig {
        bool enabled = false;                   // 默认关闭（渐进式启用）
        int session_ttl_seconds = 3600;         // 会话安全状态 TTL（秒，默认 1 小时）
        int max_sessions = 1000;                // 最大缓存会话数（防内存泄漏）
        bool s2_always_full_check = true;       // S2 历史始终全量检查（不可关闭）
        bool detect_sensitive_reference = true; // 是否检测新消息中对历史敏感信息的引用
        bool detect_history_tampering = true;   // 是否检测历史消息被篡改（消息数量减少）
    } incremental_check;

    // [S2 轮次清理] S2 轮次清理配置
    // 当历史中存在 S2 轮次时，若当前轮无 S2，则清理历史中的 S2 轮数据后再上云
    struct S2TurnCleaningConfig {
        bool enabled = true;        // 默认开启
        bool log_details = true;    // 是否输出清理详情日志
        // 清洗历史 S2 后，若当前轮路由决策为 LOCAL 但云端更适合，允许重新路由到 CLOUD
        bool allow_cloud_reroute_after_clean = false; // 默认关闭
    } s2_turn_cleaning;

    // 指标汇总输出配置（对应 §15.1.2-A）
    struct MetricsConfig {
        int summary_every_n_requests = 100; // 每 N 次请求输出一次汇总（0 表示禁用）
        int summary_every_seconds = 0;      // 每 T 秒输出一次汇总（0 表示禁用）
        int latency_sample_size = 1000;     // 延迟样本环形缓冲区大小
        int fail_reason_topn = 5;           // 脱敏失败原因 TopN 数量
    } metrics;
};

// ============================================================
// 本地模型配置结构（对应 service_config.json 中的 "local_model" 节）
// ============================================================
struct LocalModelConfig {
    bool enabled = true;  // 是否启用本地模型（默认开启，向后兼容）
};

// ============================================================
// 云端模型配置结构（对应 service_config.json 中的 "cloud_model" 节）
// ============================================================
struct CloudModelConfig {
    bool enabled = false;
    std::string base_url;
    std::string api_key;
    std::string model;
    int timeout_seconds = 30;
    // SSE 流式请求的读超时（秒），0=自动（timeout_seconds×5）
    // 多轮工具调用场景建议设置为 600s 以上，对应 service_config.json 中的 stream_timeout_seconds
    int stream_timeout_seconds = 0;
    // 云端模型上下文窗口大小（tokens），用于统一提示词优化流水线的 token 预算计算
    // 0 = 未配置，使用默认值 DEFAULT_CLOUD_CONTEXT_SIZE（32768）
    // 建议在 service_config.json 中显式配置，以匹配实际部署的云端模型
    int context_size = 0;
    static constexpr int DEFAULT_CLOUD_CONTEXT_SIZE = 32768;

    // 数据上云策略（仅当 local_model.enabled=false 且 cloud_model.enabled=true 时生效）
    struct UploadPolicyConfig {
        // true=对上云数据进行敏感性检查（默认），false=跳过检查，所有数据直接上云
        bool enable_sensitivity_check = true;
        // true=对 S1/S2 数据脱敏后再上云（默认），false=不脱敏直接上云
        bool enable_desensitization = true;
    } upload_policy;

    struct RetryConfig {
        int max = 2;
        int backoff_ms = 200;
        // 多端点总尝试次数上限（对应需求文档 §11.1 修订）
        // 0 表示使用默认值（endpoints.size() × max）
        int max_total_attempts = 0;
        // HTTP 429 限流时是否切换端点（对应需求文档 §11.4）
        // 默认 false（退避重试当前端点，不切换）
        bool retry_on_429_switch_endpoint = false;
    } retry;

    struct CircuitBreakerConfig {
        int failure_threshold = 3;  // 连续失败次数阈值（默认 3），对应 §11.4
        int cooldown_seconds = 60;  // 冷却时间（秒，默认 60），对应 §11.4 和 §15
    } circuit_breaker;

    // 云端推理限流配置（每轮任务维度，防止模型进入死循环无限消耗 tokens）
    struct RateLimitConfig {
        // 每轮任务最大推理（工具调用）次数，0=不限制，默认 20
        int max_inferences_per_task = 20;
        // 每轮任务最大累计 Token 数（prompt+completion），0=不限制，默认 0
        int max_tokens_per_task = 0;
    } rate_limit;

    struct Endpoint {
        std::string name;
        std::string base_url;
        std::string model;
    };
    std::vector<Endpoint> endpoints;

    // 云端客户端调试日志开关（[DBG] 标签日志，包含连接细节、端点选择等）
    // 默认关闭，仅用于开发调试；生产环境建议保持关闭
    bool log_debug = false;
};

// ============================================================
// 企业内网云端模型配置（对应 service_config.json 中的 "enterprise_cloud_model" 节）
// 用于 C1 级别任务路由（中等复杂度，企业内部服务器处理）
// ============================================================
struct EnterpriseCloudModelConfig {
    bool enabled = false;
    std::string base_url;
    std::string api_key;
    std::string model;
    int timeout_seconds = 60;
    // SSE 流式请求的读超时（秒），0=自动（timeout_seconds×5）
    int stream_timeout_seconds = 0;
    // 企业云模型上下文窗口大小（tokens），用于统一提示词优化流水线的 token 预算计算
    // 0 = 未配置，使用默认值 DEFAULT_ENTERPRISE_CLOUD_CONTEXT_SIZE（16384）
    // 建议在 service_config.json 中显式配置，以匹配实际部署的企业云模型
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

// ============================================================
// 系统上下文配置（对应 service_config.json 中的 "prompt_optimization.system_context" 节）
// 所有系统提示词内容均从此处读取，由 BuildSystemContext() 使用
// ============================================================

// 单个系统上下文子段落
struct SystemContextSection {
    std::string title;                  // 段落标题（如 "## Core Behavior"），空字符串表示无标题
    std::vector<std::string> lines;     // 段落内容行（每行一个字符串，不含换行符）
    bool enabled = true;                // 是否启用该段落
};

// 系统上下文配置
struct SystemContextConfig {
    std::vector<SystemContextSection> sections;  // 子段落列表（按顺序拼接）
};

// ============================================================
// Prompt 段落过滤配置（对应 service_config.json 中的 "prompt_optimization.prompt_sections" 节）
// ============================================================

// 单条段落匹配规则
struct PromptSectionRule {
    std::string title_contains;  // 标题子串匹配（大小写不敏感）
    int heading_level = 0;       // 0=任意级别，1=#，2=##，3=###
    bool include = true;         // true=保留该段落，false=丢弃
};

// 段落过滤配置
struct PromptSectionsConfig {
    std::vector<PromptSectionRule> rules;  // 匹配规则列表（按顺序匹配，第一个命中的规则生效）
    std::string default_action = "exclude"; // 未命中任何规则时的默认处理："include" 或 "exclude"
    int max_section_tokens = 0;            // 单段落最大 token 数（0=不限制）
    bool enabled = false;                  // 是否启用段落过滤（false=不过滤，不追加原始段落）
};

// ============================================================
// Prompt 优化配置（对应 service_config.json 中的 "prompt_optimization" 节）
// ============================================================

// 提示词模板配置
struct SystemPromptsConfig {
    // ── 各静态段落的启用开关 ──────────────────────────────────────────────
    // 每个字段对应 system_prompts 中同名的文本段落，false=跳过该段落不输出
    struct SectionsEnabled {
        bool identity_intro           = true;  // 身份声明（始终输出）
        bool skill_rule               = true;  // Skill 与 Tool 区分规则（仅有 SKILL 时输出）
        bool tools_intro              = true;  // 工具列表说明
        bool catalog_structured_intro = true;  // Skill Catalog 头部说明
    } sections_enabled;

    // ── Few-shot 示例的各类型启用开关 ────────────────────────────────────
    struct FewShotExamplesEnabled {
        bool enabled            = true;  // 总开关：false=完全禁用 few-shot 示例生成
        bool skill_correct_call = true;  // 正确的 Skill 调用示例（使用 read 工具加载 SKILL.md）
        bool no_skill_needed    = true;  // 无需 Skill 的普通问答示例
        // 最多生成几个 Skill 示例（取 runtime_skills 中前 N 个）
        // 0=不生成任何 Skill 示例；1=只生成第1个；2=生成前2个（默认）；以此类推
        int  max_skill_examples = 2;
    } few_shot_examples_enabled;

    // ── 静态段落内容 ──────────────────────────────────────────────────────
    std::string identity_intro;  // 身份声明，始终输出
    std::string skill_rule;      // Skill 与 Tool 区分规则，仅有 SKILL 时输出
    std::string tools_intro;
    std::string catalog_structured_intro;

    // Few-shot 示例的标题（由 BuildFewShotExamples 动态生成示例，
    // 标题从此处读取，避免硬编码中文字符串）
    std::string few_shot_header    = "## Examples\n\n";

    // Few-shot dynamic example templates (used by BuildFewShotExamples)
    // {idx} placeholder is replaced at runtime with the example index (1, 2, 3...)
    std::string few_shot_skill_title_template       = "**Example {idx} - Skill Match**\n";
    std::string few_shot_default_user_query_prefix  = "Please use the ";  // default query prefix when use_for is empty
    std::string few_shot_default_user_query_suffix  = " skill";           // default query suffix when use_for is empty
    std::string few_shot_user_label                 = "User: ";
    std::string few_shot_response_label             = "Response: ";
    std::string few_shot_correct_call_label         = "Tool(correct skill call): ";
    std::string few_shot_no_skill_title_template    = "**Example {idx} - List Skills (answer from catalog)**\n";
    std::string few_shot_no_skill_user_input        = "What skills do you have? / 有哪些skills?";
    std::string few_shot_no_skill_response          = "I have the following skills: [list from catalog above]. No tool call needed.";
};

struct PromptOptimizationConfig {
    // ── 提示词格式与工具控制 ─────────────────────────────────
    std::string skill_catalog_format = "structured";  // "structured" 或 "simple"
    bool enable_tool_whitelist = true;
    std::vector<std::string> allowed_tools = {"read", "edit", "write", "exec", "web_search", "web_fetch"};
    bool enable_skill_auto_correction = true;
    float tool_call_temperature = 0.1f;
    SystemPromptsConfig system_prompts;

    // ── 上下文窗口分配 ──────────────────────────────────────
    float output_reserve_ratio = 0.20f;    // 为输出预留的上下文比率（默认 20%）
                                            // 对应 Build() 中 context_size / 5 的硬编码

    // ── 消息数量控制 ────────────────────────────────────────
    size_t max_messages_limit = 16;         // 消息数量上限（PreFilter Step 1）
    size_t recent_window = 6;               // 最近 N 条消息视为"新消息"（受保护）

    // ── 分级压缩阈值（字符数）──────────────────────────────
    size_t old_compress_len = 300;
    size_t recent_compress_len = 600;
    size_t tool_compress_len = 400;

    // ── 压缩行为控制 ────────────────────────────────────────
    size_t min_compress_threshold = 10;     // 节省字符数低于此值则跳过压缩（避免无效操作）
    size_t tool_min_length = 300;           // 工具消息压缩后最小有效长度（低于此值则报错）

    // ── 紧急截断配置 ──────────────────────────────
    // 当最后一条 tool 消息超大导致 context overflow 时的兜底机制
    struct EmergencyTruncationConfig {
        bool enabled = true;                // 是否启用紧急截断（默认 true）
        float max_truncation_ratio = 0.40f; // 最大截断比例（0.0-1.0）；
                                            // 若需要截断的 token 数超过最后一条 tool 消息的此比例，
                                            // 则放弃截断，直接走 local_input_overflow 流程；默认 40%
        int safety_margin_tokens = 30;      // 安全余量（token 数）；截断时额外预留，防止边界情况；默认 30
    } emergency_truncation;

    // ── 原始系统提示词段落过滤配置 ──────────────────────────
    // 通过配置文件选定哪些原始提示词段落被追加到优化后的提示词中
    PromptSectionsConfig prompt_sections;

    // ── SubAgent 专用段落过滤配置 ────────────────────────────
    // 与 prompt_sections 结构相同，但仅用于 SubAgent 路径：
    // BuildSystemContext() 重建核心骨架后，从原始提示词中过滤并附加
    // SubAgent 特有段落（如 ## Workspace / ## Subagent Context / ## Runtime 等）
    PromptSectionsConfig subagent_prompt_sections;

    // ── 系统上下文配置 ──────────────────────────────────────
    // 所有系统提示词内容均从此处读取，由 BuildSystemContext() 使用
    SystemContextConfig system_context;

    // ── SpawnGuard：sessions_spawn 重复调用防护 ─────────────
    // 当 sessions_spawn 的 tool_response 包含 childSessionKey 时，
    // 在 tool_response 内容末尾注入强制等待指令，阻止小模型重复 spawn。
    struct SpawnGuardConfig {
        bool enabled = true;                    // 总开关（默认开启）
        // 注入内容的标题行（含 childSessionKey 占位符 {child_key}）
        std::string header = "[SPAWN_GUARD] Child session accepted: {child_key}";
        // 注入内容的正文（各行以 \n 分隔）
        std::string body =
            "STOP. Do NOT call sessions_spawn again.\n"
            "The child is running asynchronously. Its result will arrive as a new user message.\n"
            "Your ONLY valid next action is to output plain text (no tool calls) to acknowledge the spawn.\n"
            "Wait silently. Do not repeat the spawn.";
    } spawn_guard;

    // ── 长文本摘要化配置（Phase -1，在 prompt 构建前执行）──────
    // 对应 service_config.json 中的 "prompt_optimization.long_text_summarization" 节

    // 摘要缓存配置
    struct LongTextSummaryCacheConfig {
        bool enabled = true;            // 是否启用进程内摘要缓存
        size_t max_entries = 500;       // 最大缓存条目数（LRU 淘汰）
        size_t max_memory_mb = 50;      // 最大内存占用（MB，超出时淘汰最旧条目）
        int ttl_minutes = 60;           // 缓存条目生存时间（分钟）
    };

    // 长文本摘要化主配置
    struct LongTextSummarizationConfig {
        bool enabled = false;           // 总开关（默认关闭，需在 service_config.json 中显式开启）
        double trigger_ratio = 0.5;     // 触发阈值：content token 数 > context_size * trigger_ratio 时触发摘要
        double chunk_ratio = 0.45;      // 分块大小：chunk_token_limit = context_size * chunk_ratio
        bool summarize_user_messages = true;    // 是否对最后一条 user 文本消息执行摘要
        bool summarize_tool_responses = true;   // 是否对末尾连续 tool 消息链执行摘要
        int max_chunks = 4;             // 最大分块数（超出时截断尾部块并输出 warning）
        bool verbose_logging = false;   // 是否输出详细摘要过程日志

        // Map 阶段摘要指令（可通过配置文件覆盖）
        std::string map_instruction =
            "Summarize the following content concisely but completely. Preserve facts, file paths, "
            "code snippets, errors, numbers, and technical details.";

        // Reduce 阶段合并指令（可通过配置文件覆盖）
        std::string reduce_instruction =
            "Merge the following partial summaries into one coherent summary. Preserve all important "
            "technical information and remove duplication.";

        LongTextSummaryCacheConfig cache;
    } long_text_summarization;
};

// ============================================================
// Skill 映射配置
// ============================================================
// Skill 映射配置（仅包含名称到路径的映射）
using SkillMappings = std::unordered_map<std::string, std::string>;

// 单个 Skill 信息（运行时从客户端请求中构建）
struct SkillInfo {
    std::string name;
    std::string path;
    std::string use_for;  // 从客户端请求中获取
};

// 运行时 Skill 信息映射（包含描述）
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

    int context_size() const
    {
        return context_size_;
    }

    const std::string &get_model_path() const{return model_path_;}

    const std::string &get_model_name() const
    {
        return model_name_;
    }

    const std::string &getloraAdapter() const
    {
        return loraAdapter;
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

    int getminOutputNum() const
    {
        return minOutputNum;
    }

    float getloraAlpha() const
    {
        return loraAlpha;
    }

    json get_model_list() const;

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

    // 获取用于安全检查/复杂度评估/脱敏的模型句柄（始终使用 default 模型）
    // 默认实现：返回全局 genieModelHandle（单模型模式，与 get_genie_model_handle() 等价）
    // ModelManager 重写此方法以返回 default_model_name_ 对应的模型句柄（多模型模式）
    // 语义：无论客户端指定哪个模型，安全相关操作始终使用 default 模型，
    //       避免安全检查跟随客户端模型动态切换（例如切换到 QNN 模型后安全检查也切换到 QNN）
    virtual std::weak_ptr<ContextBase> GetDefaultModelHandle() const
    {
        return genieModelHandle;
    }

    // 检查本地模型是否可用（虚方法，支持多模型场景下的重写）
    // 默认实现：检查全局 genieModelHandle 是否有效（单模型模式）
    // ModelManager 重写此方法以检查是否有任何已加载的模型（多模型模式）
    virtual bool IsLocalModelAvailable() const
    {
        return genieModelHandle != nullptr;
    }

    // 获取 default 模型的 ModelInstanceConfig（虚方法，支持多模型场景下的重写）
    // 默认实现：返回 nullptr（单模型模式，无独立的 ModelInstanceConfig）
    // ModelManager 重写此方法以返回 default_model_name_ 对应的 ModelInstanceConfig*（多模型模式）
    // 用途：BuildLocalModelPrompt 等安全相关函数应优先使用此方法获取模型配置，
    //       而非直接读取全局 IModelConfig 的成员（后者在多模型场景下可能被 -c 参数模型污染）
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

    const PromptOptimizationConfig& GetPromptOptimizationConfig() const
    {
        return prompt_optimization_config_;
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

    // ── 运行时 Skill 映射（每次请求从客户端 <available_skills> XML 动态解析）──────
    // 由 BuildSystemContext() 在构建 prompt 时写入，
    // 供 ResponseDispatcher::AutoCorrectSkillCall() 读取以纠正错误的 SKILL 调用。
    // 线程安全：使用 shared_ptr<mutex> 保护（shared_ptr 可拷贝，不影响 IModelConfig 拷贝构造）
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
    mutable int context_size_{DEFAULT_CONTEXT_SIZE};
    // 修复：使用 json::object() 而非 json{}（默认构造）。
    // 在 nlohmann::ordered_json 中，json{} 默认构造为 array 类型（空数组），
    // 而非 null 或 object，会导致 ChatHistory::GetUserMessage 中出现 type=array 错误。
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

    // 路由与云端配置（从 service_config.json 加载）
    RoutingConfig routing_config_;
    CloudModelConfig cloud_model_config_;
    EnterpriseCloudModelConfig enterprise_cloud_model_config_;
    LocalModelConfig local_model_config_;
    
    // Prompt 优化配置
    PromptOptimizationConfig prompt_optimization_config_;

    // 运行时 Skill 映射（每次请求从客户端 <available_skills> XML 动态解析写入）
    // 供 ResponseDispatcher::AutoCorrectSkillCall() 读取，以纠正模型错误的 SKILL 直接调用
    // 使用 shared_ptr<mutex> 而非 mutex 成员，避免 IModelConfig 拷贝构造被删除
    SkillMappings runtime_skill_mappings_;
    std::shared_ptr<std::mutex> runtime_skill_mappings_mutex_{std::make_shared<std::mutex>()};

    void UpdateModeList() const;

    friend class Config;
};

#endif //MODEL_CONFIG_H
