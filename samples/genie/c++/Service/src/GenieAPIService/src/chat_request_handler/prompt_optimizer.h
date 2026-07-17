//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef PROMPT_OPTIMIZER_H
#define PROMPT_OPTIMIZER_H

#include <string>
#include <map>
#include <vector>
#include <optional>
#include "../model/model_config.h"
#include "../chat_history/chat_history.h"
#include "prompt_stats_helper.h"
#include "message_pre_filter.h"

enum class IntentType {
    TOOL_CALL,      // 工具调用
    SKILL_QUERY,    // SKILL 查询
    GENERAL_CHAT    // 普通对话
};

// Agent 类型枚举（用于区分 OpenClaw 主 agent 和子 agent）
// 判断依据：system prompt 中包含 "agent=main" → MAIN_AGENT，否则 → SUBAGENT
//
// 重要说明：Agent 类型（主/子）是请求级别的逻辑属性，与执行该请求的模型实例
// 的底层硬件类型（CPU/GPU/NPU）无关。任意硬件类型的模型均可在不同请求中
// 分别承担 MAIN_AGENT 或 SUBAGENT 角色，具体由客户端请求中携带的 model 字段
// 以及 system prompt 中的 "agent=main" 标记共同决定。
enum class AgentType {
    MAIN_AGENT,     // 主 agent：system prompt 中包含 "agent=main"（由 OpenClaw 运行时注入）
    SUBAGENT,       // 子 agent：system prompt 中不包含 "agent=main"（包括空 prompt、任务上下文等）
};

// 注意：MessageCompressionConfig 和 OptimizedMessages 已迁移到 message_pre_filter.h

class PromptOptimizer {
public:
    // 多模型并发场景：构造函数直接接受 ContextBase* 参数，避免后续 SetContext 调用
    explicit PromptOptimizer(IModelConfig& config, ContextBase* context = nullptr);
    
    // 检测 Agent 类型（主 agent 或子 agent）
    AgentType DetectAgentType(const std::string& system_prompt);

    // 优化系统提示词（主 agent 使用）
    std::string OptimizeSystemPrompt(
        const std::string& system_prompt,
        const nlohmann::ordered_json& request_data = nlohmann::ordered_json::object()
    );

    // 优化子 agent 系统提示词（替换 <available_skills> XML 为结构化 Skill Catalog，其余段落原样保留）
    std::string OptimizeSubagentSystemPrompt(
        const std::string& system_prompt,
        const nlohmann::ordered_json& request_data = nlohmann::ordered_json::object()
    );

    // 处理和优化工具定义
    std::string OptimizeToolsPrompt(
        const std::string& tool_descriptions, 
        const std::string& tool_prompt_template
    );

    // 转换 OpenAI 格式的工具调用到内部格式
    std::vector<std::string> ConvertOpenAIToolCalls(
        const nlohmann::ordered_json& tool_calls_array
    );
    
    // 优化工具响应消息
    std::string OptimizeToolResponse(
        const std::string& tool_response
    );

    // [SpawnGuard] 检测 sessions_spawn 的异步响应并注入强制等待指令
    // 若 tool_response_content 是 sessions_spawn 返回的 {status:"accepted", childSessionKey:...} JSON，
    // 则在内容末尾追加配置驱动的等待指令字符串并返回；否则原样返回。
    // 供 OptimizeToolResponse（General 格式）和 BuildHarmonyPrompt（Harmony 格式）共用。
    std::string InjectSpawnGuard(const std::string& tool_response_content) const;
    
    // ========== Harmony 格式专用优化函数 ==========
    
    // 优化 Harmony 格式的系统消息
    std::string OptimizeHarmonySystemMessage(
        const std::string& knowledge_cutoff,
        const std::string& current_date,
        const std::string& reasoning_level,
        bool has_tools
    );
    
    // 优化 Harmony 格式的 developer 消息（包含 instructions 和 tools）
    std::string OptimizeHarmonyDeveloperMessage(
        const std::string& instructions,
        const nlohmann::ordered_json& tools,
        const nlohmann::ordered_json& request_data = nlohmann::ordered_json::object()
    );
    
    // 将 OpenAI JSON 格式的工具定义转换为精简的 TypeScript 格式
    std::string ConvertToolsToOptimizedTypeScript(
        const nlohmann::ordered_json& tools
    );
    
    // 获取优化统计信息
    struct OptimizationStats {
        size_t original_tokens;
        size_t optimized_tokens;
        float savings_percent;
        IntentType detected_intent;
        std::string matched_skill;
    };
    
    OptimizationStats GetLastStats() const { return last_stats_; }
    
    // 设置 per-model 的 ContextBase（多模型并发场景）
    // 设置后，CountTokens() 将使用此 context 而非全局 model_config_.get_genie_model_handle()
    void SetContext(ContextBase* context) { context_override_ = context; }

private:
    IModelConfig& model_config_;
    // 多模型并发场景：per-model 的 ContextBase（优先于 model_config_.get_genie_model_handle()）
    ContextBase* context_override_{nullptr};
    OptimizationStats last_stats_;
    
    // 计算 token 数量
    size_t CountTokens(const std::string& text);
    
    // 从客户端请求中提取 Skills 信息
    RuntimeSkillMappings ExtractSkillsFromRequest(const nlohmann::ordered_json& request_data) const;
    
    // 构建结构化 Skill Catalog
    std::string BuildStructuredSkillCatalog(const RuntimeSkillMappings& runtime_skills) const;
    
    // 构建简单 Skill Catalog
    std::string BuildSimpleSkillCatalog(const RuntimeSkillMappings& runtime_skills) const;

    // 动态生成 few-shot 示例（基于实际 SKILL 列表，路径来自客户端原始内容）
    std::string BuildFewShotExamples(const RuntimeSkillMappings& runtime_skills) const;

    // 获取优化后的工具定义
    std::string GetOptimizedToolDefinition(const std::string& tool_name);

    // 根据 request_data["tools"] 动态生成 tools_intro 字符串。
    // 当请求中包含 tools 数组时，仅列出实际传入的工具名，避免模型看到
    // 配置文件中硬编码的、客户端并未提供的工具。
    // 若 tools 数组为空或不存在，返回空串（调用方回退到配置文件值）。
    std::string BuildDynamicToolsIntro(const nlohmann::ordered_json& request_data) const;

    // 从配置文件的 tools_intro 字符串中，过滤掉客户端未传入的工具行。
    // 若请求中无 tools 数组，则原样返回配置文件值（不过滤）。
    std::string FilterToolsIntroByRequest(const std::string& tools_intro,
                                          const nlohmann::ordered_json& request_data) const;
    
    // ========== 共享辅助函数 ==========
    
    // 生成统一的系统上下文内容（供普通模型和 Harmony 模型复用）
    // [重构] 接受 request_data，以便从客户端请求中提取 Skill 描述
    std::string BuildSystemContext(const nlohmann::ordered_json& request_data);
    
    // ========== Harmony 格式辅助函数 ==========
    
    // 优化 instructions 部分（提取核心信息）
    std::string OptimizeInstructions(const std::string& instructions);
    
    // 获取优化后的 TypeScript 工具定义
    std::string GetOptimizedTypeScriptDefinition(const std::string& tool_name);
    
    // 为未知工具生成基本的 TypeScript 定义
    std::string GenerateBasicTypeScriptDefinition(const nlohmann::ordered_json& tool);

    // ========== 原始提示词段落过滤 ==========

    // 从 source_prompt 中按 prompt_sections 配置过滤段落，返回应追加的内容字符串
    // （disabled 或无匹配段落时返回空串）
    std::string AppendFilteredSections(const std::string& source_prompt);

    // Markdown 段落结构
    struct PromptSection {
        int heading_level;        // 标题级别：1=#，2=##，3=###，0=无标题（文件头部内容）
        std::string title;        // 标题文本（不含 # 前缀和前后空白）
        std::string full_content; // 该段落的完整内容（含标题行）
    };

    // 将原始提示词按 Markdown 标题分割成顶层段落列表
    std::vector<PromptSection> ParseMarkdownSections(const std::string& prompt);

    // 根据配置规则过滤段落，返回应保留的段落内容拼接字符串
    std::string FilterSectionsByConfig(
        const std::vector<PromptSection>& sections,
        const PromptSectionsConfig& config
    );

    // 判断单个段落是否应被保留（根据规则列表和 default_action）
    bool ShouldIncludeSection(
        const PromptSection& section,
        const PromptSectionsConfig& config
    );

    // 将字符串转为小写（用于大小写不敏感匹配）
    static std::string ToLower(const std::string& s);

    // 计算 token 节省百分比（original_tokens 为 0 时返回 0，避免除零）
    static float ComputeSavingsPercent(size_t original_tokens, size_t optimized_tokens);
};

#endif // PROMPT_OPTIMIZER_H
