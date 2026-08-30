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

// D3：预算分区类别。ComputeRelevanceTokenBudget() 原为单一总预算，一条超长工具输出可以
// 把技能目录整段挤没。分区**仅在真的发生竞争时生效**（skills 与 tools 同时存在候选）：
// 此时 kSkills 至少拿到 budget_partition.skills_floor_ratio，kTools 受 tools_ratio 软上限；
// 单方存在（如既有的“只有 tools、没有 skills”场景）时两种 kind 都拿到完整总预算。
// budget_partition.enabled=false 时逐字节回退为原先的单一总预算语义。
enum class BudgetPartitionKind {
    kSkills,
    kTools
};

// D2：技能目录的三档渐进披露档位。原实现只有「全量展开 / 整条删除」两档，
// 预算不够时技能直接从目录里消失（模型再也无法得知它存在）；三档之后
// 预算耗尽只降档，只有连 kNameOnly 都放不进才真正丢弃。
enum class SkillDetailLevel {
    kNameOnly,   // L0：单行「- name -> path (一句话摘要)」
    kSummary,    // L1：Path + description 摘要 + 触发条件/tags 行
    kFull        // L2：Path + 完整 description（与 D2 引入前的渲染逐字节一致）
};

// D2：带相关性分数与披露档位的技能条目。tags 不是新协议面：服务端
// <available_skills> XML 只解析 <name>/<description>/<location>，这里的 tags 是从
// description 正文里提取出的「tags: …」/「标签: …」行。
struct ScoredSkill {
    std::string name;
    std::string description;   // 对应 SkillInfo::use_for
    std::string location;      // 对应 SkillInfo::path
    std::string tags;          // 从 description 提取的 tags 段（可为空）
    size_t score = 0;
    SkillDetailLevel level = SkillDetailLevel::kFull;
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

    // 处理和优化工具定义。token_budget 为工具部分（含模板包装后）允许占用的
    // 最大 token 数（通常是 contextSize 减去已被 system 部分占用的 token 数），
    // 压缩按 相关性筛选 -> 已知签名 -> 剥离注释 -> 极简签名 -> 按预算截断工具列表
    // 逐级降级，确保返回结果的 token 数始终不超过该预算。
    // request_data 用于提取本轮相关性关键词（BuildRelevanceKeywords），在 Tier1
    // 压缩之前先按相关性缩小候选工具集合；未命中的工具整条不出现，而不是被压缩得
    // 更简。默认空 object 保持向后兼容：拿不到 request_data 的调用点等价于
    // "无法判断相关性"，FilterToolsByRelevance 会直接跳过过滤、保留全部候选。
    std::string OptimizeToolsPrompt(
        const std::string& tool_descriptions, 
        const std::string& tool_prompt_template,
        size_t token_budget,
        const nlohmann::ordered_json& request_data = nlohmann::ordered_json::object()
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
    // request_data（可选）：用于按本轮相关性筛选候选工具（复用 FilterToolsByRelevance），
    // 默认空 object；调用方若已自行筛选过 tools（如 OptimizeHarmonyDeveloperMessage
    // 内部），不传 request_data 即可让这里的过滤天然退化为直通，避免重复过滤
    std::string ConvertToolsToOptimizedTypeScript(
        const nlohmann::ordered_json& tools,
        const nlohmann::ordered_json& request_data = nlohmann::ordered_json::object()
    );
    
    // 获取优化统计信息
    struct OptimizationStats {
        size_t original_tokens;
        size_t optimized_tokens;
        float savings_percent;
        IntentType detected_intent;
        std::string matched_skill;

        // ── 供 PromptLedger 可观测性回报使用（不新增第二套统计，直接复用本结构）──
        // tools_tier：OptimizeToolsPrompt 实际落到的降级档位，0=未经过该函数优化
        //   （无 tools 或解析失败），1~4 对应 Tier1（已知签名/基础签名）~Tier4（硬性
        //   预算截断）。由 BuildSystemContext 写入 skills_total/kept，OptimizeToolsPrompt
        //   写入 tools_tier/total/kept，二者互不覆盖对方字段。
        int tools_tier = 0;
        size_t tools_total = 0;   // 相关性过滤后进入 Tier1~4 处理的候选工具总数
        size_t tools_kept = 0;    // 最终实际保留在提示词里的工具数
        size_t skills_total = 0;  // ExtractSkillsFromRequest 解析出的 SKILL 候选总数（全量，未经相关性过滤）
        size_t skills_kept = 0;   // FilterSkillsByRelevance 筛选后实际保留的 SKILL 数
        // D3：ComputeRelevanceTokenBudget(BudgetPartitionKind::kSkills) 实际算出的
        // skills 分区预算（token），budget_partition.enabled=false 时等于单一总预算。
        size_t skills_budget_tokens = 0;
        // D2：三档渐进披露的各档技能数，三项之和恒等于 skills_kept。
        // skill_disclosure.enabled=false（或 skill_catalog_format != "structured"）时
        // 逐字节退化为 skills_l2 == skills_kept、L1/L0 恒为 0。
        size_t skills_l2 = 0;
        size_t skills_l1 = 0;
        size_t skills_l0 = 0;
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
    size_t CountTokens(const std::string& text) const;
    
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

    // ========== 相关性打分与预算贪心筛选 ==========
    // 设备侧上下文极为有限（如 Omni 模型仅 2048 token），全量携带客户端传入的
    // 全部 SKILL/工具定义会挤占宝贵的上下文空间。以下方法用于判定"本轮问题是否
    // 真的用得上某个 skill/tool"：命中才保留在提示词里，未命中整条删除（不是
    // 压缩，是不出现）。全部为纯字符串/规则匹配，不引入向量/embedding 语义匹配，
    // 保证零额外推理延迟。

    // 复用 PromptOptimizationConfig::recent_window 语义（与 message_pre_filter 中
    // "最近 N 条非 system 消息视为新消息"一致），从 request_data["messages"] 里
    // 提取该窗口内 role=="user" 的消息文本（content 可能是字符串也可能是 OpenAI
    // 多段数组，统一通过 SecurityUtils::ExtractMessageContentText 读取），小写化后
    // 按非字母数字字符（含中文/全角标点、空格等）为边界切词，返回去重后的关键词列表。
    std::vector<std::string> BuildRelevanceKeywords(const nlohmann::ordered_json& request_data) const;

    // 对 name/description 与 keywords 打分：
    // - name 按 '-'/'_' 拆分成子词，每个子词若与任一 keyword 发生子串双向匹配
    //   （大小写不敏感）则命中，命中一次加 relevance_filter.name_token_weight；
    // - description 做关键词命中计数（子串匹配），每命中一个 keyword 加
    //   relevance_filter.description_keyword_weight。
    // - D4：tags 非空时额外做一轮关键词命中计数，每命中一个加 relevance_filter.tag_weight
    //   （tag_weight=0 或 tags 为空时逐字节等价于 D4 引入前）。
    // 返回总分，0 表示完全不相关。
    size_t ScoreRelevance(const std::string& name, const std::string& description,
                          const std::vector<std::string>& keywords,
                          const std::string& tags = std::string()) const;

    // 对 all_skills 中每个 SKILL 用 ScoreRelevance() 打分，过滤掉零分项，按分数
    // 降序排序后按 token_budget 贪心保留，超预算即停止。all_skills 为空时返回空集；
    // keywords 为空时（通常意味着调用方未提供/本轮窗口内没有任何用户文本，视为
    // "无法判断相关性"而非"确定无关"）直接跳过过滤、原样返回 all_skills 全量，
    // 避免误伤——这是 Step2 接入调用链时对 Step1 空 keywords 语义的唯一微调。
    RuntimeSkillMappings FilterSkillsByRelevance(const RuntimeSkillMappings& all_skills,
                                                 const std::vector<std::string>& keywords,
                                                 size_t token_budget) const;

    // D2：按相关性分数 + skills 分区预算给每个技能分配披露档位。
    // 与 FilterSkillsByRelevance 的关键区别：预算耗尽时**降档**（L2→L1→L0）而不是
    // 整条删除，只有连 L0 单行都放不进预算时才真正丢弃。零分丢弃与
    // zero_hit_keep_all 全零分兜底的语义与 FilterSkillsByRelevance 完全一致（不另起一套
    // 判据）；区别只在“保留下来的那些怎么展示”。返回值按分数降序（同分按名称）。
    std::vector<ScoredSkill> AssignSkillDetailLevels(const RuntimeSkillMappings& all_skills,
                                                     const std::vector<std::string>& keywords,
                                                     size_t skills_token_budget) const;

    // D2：渲染单条技能在指定档位下的目录文本（含末尾换行）。
    std::string RenderSkillEntry(const ScoredSkill& skill) const;

    // D2：按档位渲染整份结构化 Skill Catalog（头部说明与
    // BuildStructuredSkillCatalog 完全一致，只是条目体按档位变化）。
    std::string BuildLeveledSkillCatalog(const std::vector<ScoredSkill>& skills) const;

    // 对 tools_array（OpenAI tools 格式）中每个工具用 ScoreRelevance() 打分，
    // 过滤掉零分项，按分数降序排序后按 token_budget 贪心保留 Top-K，返回筛选后的
    // json 数组（保持原始 tool 对象结构不变，只是子集）。tools_array 为空时返回
    // 空数组；keywords 为空时同 FilterSkillsByRelevance，直接跳过过滤、原样返回
    // tools_array 全量。
    nlohmann::ordered_json FilterToolsByRelevance(const nlohmann::ordered_json& tools_array,
                                                  const std::vector<std::string>& keywords,
                                                  size_t token_budget) const;

    // 计算相关性筛选可用的 token 预算：复用现有"可用上下文空间"推导（context_size
    // 减去按 output_reserve_ratio 预留的输出空间），与 model_input_builder.h 中
    // 计算 OptimizeToolsPrompt 的 tools_budget 采用的是同一套已有概念，避免为
    // 相关性筛选引入新的强耦合硬编码魔数。供 BuildSystemContext/
    // BuildDynamicToolsIntro/FilterToolsIntroByRequest/OptimizeHarmonyDeveloperMessage/
    // ConvertToolsToOptimizedTypeScript 共用（这些函数自身未接收显式 token_budget
    // 参数，需要自行推导）。
    // D3：kind 默认 kTools（历史上大多数调用点都是工具预算）；skills 路径显式传 kSkills。
    // request_data 用于判定是否真的发生了预算竞争（skills 与 tools 同时存在候选）；
    // 传 nullptr 或无竞争时直接返回单一总预算，绝不无故削减 D3 引入前的既有行为。
    size_t ComputeRelevanceTokenBudget(BudgetPartitionKind kind = BudgetPartitionKind::kTools,
                                       const nlohmann::ordered_json* request_data = nullptr) const;

    // D3：预算竞争判定——仅当 system prompt 里带了 <available_skills> 目录
    // **且** 请求带了非空 tools 数组时才算竞争。单方存在时不分区，避免把
    // “17 工具 Schema、无技能”这类既有场景的工具预算静默削到 tools_ratio。
    bool HasBudgetContention(const nlohmann::ordered_json& request_data) const;

    // ========== 共享辅助函数 ==========
    
    // 生成统一的系统上下文内容（供普通模型和 Harmony 模型复用）
    // [重构] 接受 request_data，以便从客户端请求中提取 Skill 描述
    // out_intent/out_matched_skill（可选，默认 nullptr）：将本轮相关性筛选得到的
    // 意图判定结果回传给调用方（OptimizeSystemPrompt/OptimizeSubagentSystemPrompt
    // 用它们填充 OptimizationStats::detected_intent/matched_skill），调用方不关心
    // 时传 nullptr 即可
    std::string BuildSystemContext(const nlohmann::ordered_json& request_data,
                                   IntentType* out_intent = nullptr,
                                   std::string* out_matched_skill = nullptr);
    
    // ========== Harmony 格式辅助函数 ==========
    
    // 优化 instructions 部分（提取核心信息）
    std::string OptimizeInstructions(const std::string& instructions);
    
    // 获取优化后的 TypeScript 工具定义
    std::string GetOptimizedTypeScriptDefinition(const std::string& tool_name);
    
    // 为未知工具生成基本的 TypeScript 定义
    std::string GenerateBasicTypeScriptDefinition(const nlohmann::ordered_json& tool);

    // 剥离工具签名文本中的 `//` 注释（整行注释与行内注释），只保留类型签名骨架，
    // 用于 OptimizeToolsPrompt 压缩预算不足时的降级
    std::string StripToolCommentLines(const std::string& text);

    // 生成单个工具的极简签名 "name(param1, param2?)"（不含类型/描述），
    // 作为 OptimizeToolsPrompt 压缩预算仍不足时的最终降级格式
    std::string BuildMinimalToolSignature(const nlohmann::ordered_json& tool);

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
