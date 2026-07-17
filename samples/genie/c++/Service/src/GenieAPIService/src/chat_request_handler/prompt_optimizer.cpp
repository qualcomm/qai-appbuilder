//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "prompt_optimizer.h"
#include "prompt_stats_helper.h"
#include "../context/context_base.h"
#include "../gateway/security/security_utils.h"
#include "log.h"
#include "utils.h"
#include <algorithm>
#include <cctype>
#include <sstream>
#include <ctime>
#include <regex>
#include <unordered_map>
#include <unordered_set>
#include <nlohmann/json.hpp>

using json = nlohmann::ordered_json;

PromptOptimizer::PromptOptimizer(IModelConfig& config, ContextBase* context)
    : model_config_(config), context_override_(context)
{
}

float PromptOptimizer::ComputeSavingsPercent(size_t original_tokens, size_t optimized_tokens)
{
    return (original_tokens > 0)
        ? 100.0f * (1.0f - static_cast<float>(optimized_tokens) / static_cast<float>(original_tokens))
        : 0.0f;
}

AgentType PromptOptimizer::DetectAgentType(const std::string& system_prompt)
{
    // 判断依据：OpenClaw 主 Agent 的 system prompt 中会由运行时注入
    // "## Runtime" 块，其中包含 "agent=main" 字段。
    // 只要匹配到 "agent=main" 就认定为主 Agent，否则一律视为子 Agent。
    //
    // 示例（主 Agent Runtime 块）：
    //   ## Runtime
    //   Runtime: agent=main | host=... | model=... | ...
    //
    // 子 Agent 的请求通常不携带完整 system prompt（或为空），
    // 也不会包含 "agent=main"，因此会走 SUBAGENT 分支。
    if (system_prompt.find("agent=main") != std::string::npos) {
        My_Log{My_Log::Level::kInfo} << "[AgentType] Detected: MAIN_AGENT (agent=main found in system prompt)" << std::endl;
        return AgentType::MAIN_AGENT;
    }

    // 未匹配到 agent=main，视为子 Agent（包括空 system prompt、子 Agent 任务上下文等情况）
    My_Log{My_Log::Level::kInfo} << "[AgentType] Detected: SUBAGENT (agent=main not found)" << std::endl;
    return AgentType::SUBAGENT;
}

std::string PromptOptimizer::OptimizeSubagentSystemPrompt(
    const std::string& system_prompt,
    const nlohmann::ordered_json& request_data)
{
    try {
        last_stats_.original_tokens = CountTokens(system_prompt);
        last_stats_.detected_intent = IntentType::GENERAL_CHAT;
        last_stats_.matched_skill = "";

        // ── Step 1: 模板重建（与 MainAgent 的 OptimizeSystemPrompt 完全相同）────────
        // 调用 BuildSystemContext() 重建核心骨架：
        //   identity_intro + skill_rule(有SKILL时) + tools_intro + skill_catalog + few_shot_examples
        // 这与 MainAgent 的处理完全一致，复用同一套配置驱动的模板逻辑。
        // BuildSystemContext() 内部会调用 ExtractSkillsFromRequest() 解析 Skills，
        // 并调用 SetRuntimeSkillMappings() 写入运行时映射（供 AutoCorrectSkillCall 使用）。
        std::string optimized = BuildSystemContext(request_data);

        // ── Step 2: 从原始提示词中过滤并附加 SubAgent 特有段落 ──────────────────────
        // 使用独立的 subagent_prompt_sections 配置（区别于 MainAgent 的 prompt_sections），
        // 保留 SubAgent 特有的上下文段落：
        //   ## Workspace / ## Subagent Context / ## Current Date & Time /
        //   ## Inbound Context / ## Runtime / ## Workspace Files (injected)
        // 丢弃已由 BuildSystemContext() 替代的段落：
        //   ## Tooling / ## Tool Call Style / ## Safety / ## OpenClaw CLI / ## Skills
        const PromptSectionsConfig& subagent_cfg = model_config_.GetSubagentPromptSectionsConfig();
        if (subagent_cfg.enabled) {
            std::vector<PromptSection> sections = ParseMarkdownSections(system_prompt);
            std::string appended = FilterSectionsByConfig(sections, subagent_cfg);
            if (!appended.empty()) {
                optimized += appended;
            }
        }

        // ── Step 3: 统计 ──────────────────────────────────────────────────────────
        last_stats_.optimized_tokens = CountTokens(optimized);
        last_stats_.savings_percent =
            ComputeSavingsPercent(last_stats_.original_tokens, last_stats_.optimized_tokens);

        My_Log{My_Log::Level::kInfo} << "[SubagentOptimizer] Original: " << last_stats_.original_tokens
                                      << " tokens, Optimized: " << last_stats_.optimized_tokens
                                      << " tokens, Savings: " << last_stats_.savings_percent << "%" << std::endl;
        return optimized;

    } catch (const std::exception& e) {
        My_Log{} << "[SubagentOptimizer] Optimization failed: " << e.what() << std::endl;
        My_Log{} << "[SubagentOptimizer] Falling back to original prompt" << std::endl;
        return system_prompt;
    }
}

std::string PromptOptimizer::OptimizeSystemPrompt(
    const std::string& system_prompt,
    const nlohmann::ordered_json& request_data)
{
    try {
        // 1. 使用统一的系统上下文（从配置文件 system_context.sections 读取）
        // BuildSystemContext 内部会调用 ExtractSkillsFromRequest 动态提取 SKILL 信息，
        // 并调用 BuildFewShotExamples 动态生成示例，无需在此重复调用
        std::string optimized = BuildSystemContext(request_data);

        // 2. 根据 prompt_sections 配置，从原始提示词中提取额外段落并追加
        std::string filtered = AppendFilteredSections(system_prompt);
        if (!filtered.empty()) {
            optimized += "# Additional Context\n\n";
            optimized += filtered;
        }

        // 3. 记录统计信息
        last_stats_.original_tokens = CountTokens(system_prompt);
        last_stats_.optimized_tokens = CountTokens(optimized);
        last_stats_.savings_percent =
            ComputeSavingsPercent(last_stats_.original_tokens, last_stats_.optimized_tokens);
        last_stats_.detected_intent = IntentType::GENERAL_CHAT;
        last_stats_.matched_skill = "";

        // 4. 输出日志
        My_Log{My_Log::Level::kDebug} << "[Optimizer] Original: " << last_stats_.original_tokens
                                       << " tokens, Optimized: " << last_stats_.optimized_tokens
                                       << " tokens, Savings: " << last_stats_.savings_percent << "%" << std::endl;

        return optimized;

    } catch (const std::exception& e) {
        My_Log{} << "Prompt optimization failed: " << e.what() << std::endl;
        My_Log{} << "Falling back to original prompt" << std::endl;
        return system_prompt;
    }
}

std::string PromptOptimizer::BuildSystemContext(const nlohmann::ordered_json& request_data)
{
    std::ostringstream oss;

    const auto& config = model_config_.GetPromptOptimizationConfig();
    const auto& se = config.system_prompts.sections_enabled;

    // 0. 提前解析 runtime_skills，供后续各段落条件判断使用
    // （原位于步骤 5，提前至此以便 skill_rule 能根据是否有 SKILL 决定是否输出）
    RuntimeSkillMappings runtime_skills = ExtractSkillsFromRequest(request_data);

    // 1a. 身份声明（始终输出，与是否有 SKILL 无关）
    if (se.identity_intro && !config.system_prompts.identity_intro.empty()) {
        oss << config.system_prompts.identity_intro;
    }

    // 1b & 2. Skill 规则 + 工具列表（仅在有 SKILL 时输出）
    // 原因：tools_intro 的作用是配合 skill_rule 告知模型"哪些是工具、哪些是 Skill"，
    // 当没有 SKILL 时，这两段提示均无意义，省略可减少 token 消耗并避免引入不存在的概念。
    if (!runtime_skills.empty()) {
        // 1b. Skill 与 Tool 区分规则
        if (se.skill_rule && !config.system_prompts.skill_rule.empty()) {
            oss << config.system_prompts.skill_rule;
        }

        // 2. 工具列表
        // 优先级：
        //   a. 配置文件有 tools_intro → 使用配置文件值，但过滤掉客户端未传入的工具行
        //   b. 配置文件无 tools_intro → 根据客户端 tools 数组动态生成
        if (se.tools_intro) {
            std::string tools_intro_str;
            if (!config.system_prompts.tools_intro.empty()) {
                // 配置文件有值：以配置文件为准，过滤掉客户端未传入的工具
                tools_intro_str = FilterToolsIntroByRequest(config.system_prompts.tools_intro, request_data);
            } else {
                // 配置文件无值：动态生成
                tools_intro_str = BuildDynamicToolsIntro(request_data);
            }
            if (!tools_intro_str.empty()) {
                oss << tools_intro_str;
            }
        }
    }
    
    // 3. 系统上下文内容（从 sections 读取，输出在 Skill Catalog 之前）
    // 顺序说明：system_context.sections 中的 Core Behavior 等段落包含对 Skill 列表的引导语
    // （如"** 重要：如下 Available Skills 列表中的 Skills..."），必须在 Skill Catalog 之前输出
    const SystemContextConfig& ctx_cfg = model_config_.GetSystemContextConfig();
    for (const auto& sec : ctx_cfg.sections) {
        if (!sec.enabled) continue;

        // 跳过已处理的段落：
        // - Tool Usage Guidelines：由 OptimizeHarmonyDeveloperMessage() 在工具存在时单独处理
        // - Examples：由下方 BuildFewShotExamples() 动态生成替代，避免重复输出硬编码内容
        // 注意：Core Behavior 不跳过，应正常输出（旧代码行为）
        if (sec.title.find("Tool Usage Guidelines") != std::string::npos ||
            sec.title.find("Examples") != std::string::npos) {
            continue;
        }

        if (!sec.title.empty()) oss << sec.title << "\n";
        for (const auto& line : sec.lines) oss << line << "\n";
        if (!sec.title.empty() || !sec.lines.empty()) oss << "\n";
    }

    // 5 & 6. Skill Catalog + Few-shot 示例
    // runtime_skills 已在步骤 0 提前解析，此处直接使用

    // 将运行时 SKILL 映射（name->path）写入 model_config_，
    // 供 ResponseDispatcher::AutoCorrectSkillCall() 在推理完成后读取，
    // 以纠正模型错误地将 SKILL 名当作工具直接调用的情况
    if (!runtime_skills.empty()) {
        SkillMappings name_to_path;
        for (const auto& [name, info] : runtime_skills) {
            if (!info.path.empty()) {
                name_to_path[name] = info.path;
            }
        }
        model_config_.SetRuntimeSkillMappings(name_to_path);
    }

    if (!runtime_skills.empty()) {
        // 5. Skill Catalog
        // catalog_structured_intro 的开关在 BuildStructuredSkillCatalog 内部读取，
        // 但 Skill Catalog 的条目列表（路径/描述）始终输出（仅头部说明受开关控制）
        const auto& opt_config = model_config_.GetPromptOptimizationConfig();
        std::string skill_section;
        if (opt_config.skill_catalog_format == "structured") {
            skill_section = BuildStructuredSkillCatalog(runtime_skills);
        } else {
            skill_section = BuildSimpleSkillCatalog(runtime_skills);
        }
        if (!skill_section.empty()) {
            oss << skill_section;
        }

        // 6. Few-shot 示例（动态生成，受 few_shot_examples_enabled.enabled 总开关控制）
        if (opt_config.system_prompts.few_shot_examples_enabled.enabled) {
            std::string few_shot = BuildFewShotExamples(runtime_skills);
            if (!few_shot.empty()) {
                oss << few_shot;
            }
        } else {
            My_Log{My_Log::Level::kInfo} << "[BuildSystemContext] few_shot_examples_enabled.enabled=false, skipping few-shot examples" << std::endl;
        }
    }

    return oss.str();
}

size_t PromptOptimizer::CountTokens(const std::string& text) {
    // 修复：多模型场景下优先使用 context_override_（per-model 的 ContextBase），
    // 而非 model_config_.get_genie_model_handle()（全局单模型句柄）
    if (context_override_) {
        return context_override_->TokenLength(text);
    }
    auto handle = model_config_.get_genie_model_handle().lock();
    if (handle) {
        return handle->TokenLength(text);
    }
    // 如果无法获取 handle，使用粗略估算（1 token ≈ 4 字符）
    return text.length() / 4;
}

// 从 system prompt 中解析 <available_skills> XML，返回完整的 RuntimeSkillMappings
// 客户端 XML 格式：
//   <skill>
//     <name>skill-name</name>
//     <description>...</description>
//     <location>~/.openclaw/skills/skill-name/SKILL.md</location>
//   </skill>
// 注意：路径字段为 <location>（不是 <path>），直接从客户端原始内容中获取，不依赖配置文件
static RuntimeSkillMappings ParseAvailableSkillsXml(const std::string& system_prompt) {
    RuntimeSkillMappings skills;

    size_t block_start = system_prompt.find("<available_skills>");
    if (block_start == std::string::npos) {
        My_Log{My_Log::Level::kInfo} << "[ParseAvailableSkillsXml] <available_skills> block not found in system prompt (len="
                                      << system_prompt.size() << ")" << std::endl;
        return skills;
    }
    size_t block_end = system_prompt.find("</available_skills>", block_start);
    if (block_end == std::string::npos) {
        My_Log{My_Log::Level::kInfo} << "[ParseAvailableSkillsXml] </available_skills> closing tag not found (block_start="
                                      << block_start << ", prompt_len=" << system_prompt.size() << ")" << std::endl;
        return skills;
    }

    std::string xml_block = system_prompt.substr(block_start, block_end + 19 - block_start);

    size_t pos = 0;
    while ((pos = xml_block.find("<skill>", pos)) != std::string::npos) {
        size_t end_skill = xml_block.find("</skill>", pos);
        if (end_skill == std::string::npos) break;

        std::string skill_block = xml_block.substr(pos, end_skill - pos);

        auto extract_tag = [&](const std::string& tag) -> std::string {
            std::string start_tag = "<" + tag + ">";
            std::string end_tag   = "</" + tag + ">";
            size_t s = skill_block.find(start_tag);
            size_t e = skill_block.find(end_tag);
            if (s != std::string::npos && e != std::string::npos) {
                std::string val = skill_block.substr(s + start_tag.size(), e - s - start_tag.size());
                return SecurityUtils::TrimWhitespace(val);
            }
            return "";
        };

        std::string name     = extract_tag("name");
        std::string desc     = extract_tag("description");
        std::string location = extract_tag("location");

        // name 和 location 都必须存在才构成有效的 skill 记录
        if (!name.empty() && !location.empty()) {
            SkillInfo info;
            info.name    = name;
            info.path    = location;  // 直接使用客户端提供的路径，不依赖配置文件
            info.use_for = desc;      // 描述可以为空
            skills[name] = info;
        }

        pos = end_skill + 8;
    }

    My_Log{My_Log::Level::kInfo} << "[ParseAvailableSkillsXml] Parsed " << skills.size()
                                  << " skills from <available_skills> XML" << std::endl;
    return skills;
}

RuntimeSkillMappings PromptOptimizer::ExtractSkillsFromRequest(const nlohmann::ordered_json& request_data) const {
    RuntimeSkillMappings runtime_skills;

    // 从 messages 中的 system prompt 解析 <available_skills> XML ────────────────
    // OpenClaw 客户端将 skill 信息（含 <location> 路径）以 XML 格式嵌入 system prompt，
    // 路径和描述均从客户端原始内容中获取，不依赖配置文件。
    if (request_data.contains("messages") && request_data["messages"].is_array()) {
        for (const auto& msg : request_data["messages"]) {
            if (!msg.contains("role") || !msg["role"].is_string()) continue;
            if (msg["role"].get<std::string>() != "system") continue;
            // 诊断：记录 content 字段的类型，便于排查 is_string() 返回 false 的情况
            if (!msg.contains("content")) {
                My_Log{My_Log::Level::kInfo} << "[ExtractSkillsFromRequest] system msg has no 'content' field, skipping" << std::endl;
                break;
            }
            if (!msg["content"].is_string()) {
                My_Log{My_Log::Level::kInfo} << "[ExtractSkillsFromRequest] system msg content is not a string (type="
                                              << msg["content"].type_name() << "), skipping" << std::endl;
                break;
            }

            const std::string& content = msg["content"].get_ref<const std::string&>();
            My_Log{My_Log::Level::kInfo} << "[ExtractSkillsFromRequest] system msg content len=" << content.size()
                                          << ", has_available_skills=" << (content.find("<available_skills>") != std::string::npos ? "yes" : "no")
                                          << std::endl;
            runtime_skills = ParseAvailableSkillsXml(content);
            if (!runtime_skills.empty()) {
                My_Log{My_Log::Level::kInfo} << "[ExtractSkillsFromRequest] Got " << runtime_skills.size()
                                              << " skills from <available_skills> XML in system prompt" << std::endl;
            }
            break;  // 只处理第一个 system 消息
        }
    }

    return runtime_skills;
}

std::string PromptOptimizer::BuildStructuredSkillCatalog(
    const RuntimeSkillMappings& runtime_skills
) const {
    std::ostringstream oss;
    
    const auto& config = model_config_.GetPromptOptimizationConfig();
    // 使用配置中的头部说明（受 sections_enabled.catalog_structured_intro 开关控制）
    if (config.system_prompts.sections_enabled.catalog_structured_intro &&
        !config.system_prompts.catalog_structured_intro.empty()) {
        oss << config.system_prompts.catalog_structured_intro;
    }
    
    for (const auto& [skill_name, skill_info] : runtime_skills) {
        oss << "Path: " << skill_info.path << "\n";
        // 使用从客户端请求中获取的描述
        if (!skill_info.use_for.empty()) {
            oss << "Use for: " << skill_info.use_for << "\n";
        }
        oss << "\n";
    }
    
    return oss.str();
}

std::string PromptOptimizer::BuildSimpleSkillCatalog(
    const RuntimeSkillMappings& runtime_skills
) const {
    std::ostringstream oss;
    
    for (const auto& [skill_name, skill_info] : runtime_skills) {
        // 使用从客户端获取的描述或默认名称
        std::string display_name = skill_info.use_for.empty() ? skill_name : skill_info.use_for;
        oss << "- " << display_name << " -> " << skill_info.path << "\n";
    }
    
    return oss.str();
}

std::string PromptOptimizer::BuildFewShotExamples(const RuntimeSkillMappings& runtime_skills) const {
    // 动态生成 few-shot 示例，基于实际解析到的 SKILL 列表
    // 从 runtime_skills 中取前两个 SKILL 作为示例，路径直接来自客户端 <available_skills> XML
    if (runtime_skills.empty()) {
        return "";
    }

    // 从配置文件读取标题、前言及动态示例模板（避免硬编码中文字符串）
    const auto& sys_prompts = model_config_.GetPromptOptimizationConfig().system_prompts;
    const auto& fe = sys_prompts.few_shot_examples_enabled;

    // 检查是否有任何示例类型被启用（避免输出空的 Examples 段落）
    bool any_enabled = fe.skill_correct_call || fe.no_skill_needed;
    if (!any_enabled) {
        My_Log{My_Log::Level::kInfo} << "[BuildFewShotExamples] All example types disabled, skipping" << std::endl;
        return "";
    }

    // 辅助函数：转义 JSON 字符串中的反斜杠（Windows 路径需要）
    auto escape_path = [](const std::string& path) -> std::string {
        std::string out;
        out.reserve(path.size() * 2);
        for (char c : path) {
            if (c == '\\') out += "\\\\";
            else out += c;
        }
        return out;
    };

    std::ostringstream oss;

    // 辅助函数：将模板字符串中的 {idx} 替换为实际序号
    auto apply_idx = [](const std::string& tmpl, int idx) -> std::string {
        std::string result = tmpl;
        const std::string placeholder = "{idx}";
        size_t pos = result.find(placeholder);
        if (pos != std::string::npos) {
            result.replace(pos, placeholder.size(), std::to_string(idx));
        }
        return result;
    };

    oss << sys_prompts.few_shot_header;

    // 动态生成 Skill 示例：取前 max_skill_examples 个 SKILL
    // max_skill_examples=0 时不生成任何 Skill 示例；1=只生成第1个；2=前2个（默认）；以此类推
    int example_idx = 1;
    const int max_skill = fe.max_skill_examples;
    if (fe.skill_correct_call && max_skill > 0) {
        for (const auto& [skill_name, skill_info] : runtime_skills) {
            if (example_idx > max_skill) break;

            oss << apply_idx(sys_prompts.few_shot_skill_title_template, example_idx);
            oss << "```\n";

            // 用 use_for 描述作为用户查询示例（截取前50字符避免过长）
            std::string user_query = skill_info.use_for;
            if (user_query.empty()) {
                user_query = sys_prompts.few_shot_default_user_query_prefix
                           + skill_name
                           + sys_prompts.few_shot_default_user_query_suffix;
            } else if (user_query.size() > 50) {
                // 截取到最近的空格处（避免使用多字节中文字符 '，' 进行 rfind，
                // 因为 char 类型的 rfind 在字节层面搜索，可能匹配到多字节序列的中间字节，
                // 导致截断位置落在 UTF-8 字符中间，产生无效 UTF-8 序列）
                size_t cut = user_query.rfind(' ', 50);
                if (cut == std::string::npos) cut = 50;
                // 使用 UTF-8 安全截断，确保不会在多字节字符中间截断
                user_query = safe_utf8_truncate(user_query, cut, "...");
            }

            oss << sys_prompts.few_shot_user_label << "\"" << user_query << "\"\n";

            // 正确调用示例
            oss << sys_prompts.few_shot_correct_call_label
                << "<tool_call>{\"name\": \"read\", \"arguments\": "
                << "{\"path\": \"" << escape_path(skill_info.path) << "\"}}</tool_call>\n";

            oss << "```\n\n";
            ++example_idx;
        }
    } else {
        My_Log{My_Log::Level::kInfo} << "[BuildFewShotExamples] skill_correct_call disabled, skipping skill examples" << std::endl;
    }

    // 追加"无需技能"的示例（受 no_skill_needed 开关控制）
    if (fe.no_skill_needed) {
        oss << apply_idx(sys_prompts.few_shot_no_skill_title_template, example_idx);
        oss << "```\n";
        oss << sys_prompts.few_shot_user_label << "\"" << sys_prompts.few_shot_no_skill_user_input << "\"\n";
        oss << sys_prompts.few_shot_response_label << sys_prompts.few_shot_no_skill_response << "\n";
        oss << "```\n\n";
    } else {
        My_Log{My_Log::Level::kInfo} << "[BuildFewShotExamples] no_skill_needed disabled, skipping no-skill example" << std::endl;
    }

    return oss.str();
}

std::string PromptOptimizer::OptimizeToolsPrompt(
    const std::string& tool_descriptions,
    const std::string& tool_prompt_template)
{
    My_Log{My_Log::Level::kDebug} << "[Optimizer] Processing tool descriptions" << std::endl;
    
    std::string result;
    
    // tool_descriptions 现在直接是原始 JSON 数组字符串
    try {
        json tools_array = json::parse(tool_descriptions);
        
        if (tools_array.is_array()) {
            std::string optimized_tools;
            size_t original_tokens = CountTokens(tool_descriptions);
            
            // 遍历每个工具，使用 GetOptimizedToolDefinition 优化
            for (const auto& tool : tools_array) {
                std::string tool_name;
                if (tool.contains("function") && tool["function"].contains("name")) {
                    tool_name = tool["function"]["name"].get<std::string>();
                }
                
                // 尝试获取优化后的定义
                std::string optimized_def = GetOptimizedToolDefinition(tool_name);
                
                if (!optimized_def.empty()) {
                    // 使用优化后的定义
                    optimized_tools += optimized_def + "\n";
                    My_Log{My_Log::Level::kDebug} << "[Optimizer] Optimized tool: " << tool_name << std::endl;
                } else {
                    // 如果是 image 工具，过滤掉
                    if (tool_name == "image") {
                        My_Log{My_Log::Level::kDebug} << "[Optimizer] Filtered out 'image' tool" << std::endl;
                        continue;
                    }
                    // 其他未知工具，保留原始定义
                    optimized_tools += tool.dump() + "\n";
                    My_Log{My_Log::Level::kDebug} << "[Optimizer] Kept original definition for: " << tool_name << std::endl;
                }
            }
            
            // 计算 token 节省
            size_t optimized_tokens = CountTokens(optimized_tools);
            float savings = ComputeSavingsPercent(original_tokens, optimized_tokens);
            
            My_Log{My_Log::Level::kDebug} << "[Optimizer] Tools - Original: " << original_tokens
                                           << " tokens, Optimized: " << optimized_tokens
                                           << " tokens, Savings: " << savings << "%" << std::endl;
            
            // 使用传入的模板包装优化后的工具（复用 prompt.h 中的定义）
            // 去掉 optimized_tools 末尾多余的换行，避免 </tools> 前出现空行
            while (!optimized_tools.empty() && optimized_tools.back() == '\n') {
                optimized_tools.pop_back();
            }
            if (!tool_prompt_template.empty()) {
                result = str_replace(tool_prompt_template, "{tool_descs}", optimized_tools);
            } else {
                // 如果没有传入模板，直接添加优化后的工具
                result = optimized_tools;
            }
        }
    } catch (const std::exception& e) {
        My_Log{} << "Failed to parse/optimize tools: " << e.what() << std::endl;
        // 解析失败，使用原始工具描述
        if (!tool_prompt_template.empty()) {
            result = str_replace(tool_prompt_template, "{tool_descs}", tool_descriptions);
        } else {
            result = tool_descriptions;
        }
    }
    
    return result;
}

std::string PromptOptimizer::BuildDynamicToolsIntro(const nlohmann::ordered_json& request_data) const
{
    // 从请求的顶层 "tools" 数组中提取工具名，生成与配置文件 tools_intro 格式
    // 完全一致的字符串，但只包含客户端实际传入的工具。
    //
    // 期望格式（与 tools_intro 配置保持一致）：
    //   You can only call these tools:
    //   - read(path, offset?, limit?)
    //   - edit(path, edits:[{oldText, newText}])
    //   - write(path, content)
    //
    //   Never call any other tool name.
    //
    // 工具的参数签名从 GetOptimizedToolSignature() 获取（与 GetOptimizedToolDefinition
    // 使用相同的预定义表，保持一致性）。未知工具使用 "name(...)" 占位格式。

    if (!request_data.contains("tools") || !request_data["tools"].is_array()
        || request_data["tools"].empty()) {
        return "";  // 无 tools → 调用方回退到配置文件硬编码值
    }

    const auto& tools_array = request_data["tools"];

    // 收集工具名列表（保持请求中的顺序）
    std::vector<std::string> tool_names;
    for (const auto& tool : tools_array) {
        if (!tool.contains("function") || !tool["function"].contains("name")) continue;
        std::string name = tool["function"]["name"].get<std::string>();
        if (!name.empty()) {
            tool_names.push_back(name);
        }
    }

    if (tool_names.empty()) {
        return "";
    }

    // 预定义的工具参数签名（与 GetOptimizedToolDefinition 保持一致）
    static const std::unordered_map<std::string, std::string> kToolSignatures = {
        {"read",       "read(path, offset?, limit?)"},
        {"write",      "write(path, content)"},
        {"edit",       "edit(path, edits:[{oldText, newText}])"},
        {"exec",       "exec(command, timeout?)"},
        {"web_search", "web_search(query, count?, country?, freshness?)"},
        {"web_fetch",  "web_fetch(url, extractMode?, maxChars?)"},
        {"browser",    "browser(action, ...)"},
        {"cron",       "cron(action, ...)"},
    };

    std::ostringstream oss;
    oss << "You can only call these tools:\n";
    for (const auto& name : tool_names) {
        auto it = kToolSignatures.find(name);
        if (it != kToolSignatures.end()) {
            oss << "- " << it->second << "\n";
        } else {
            oss << "- " << name << "(...)\n";
        }
    }
    oss << "\nNever call any other tool name.\n\n";

    My_Log{My_Log::Level::kInfo}
        << "[BuildDynamicToolsIntro] Generated tools_intro for "
        << tool_names.size() << " tool(s)" << std::endl;

    return oss.str();
}

std::string PromptOptimizer::FilterToolsIntroByRequest(
    const std::string& tools_intro,
    const nlohmann::ordered_json& request_data) const
{
    // 若请求中无 tools 数组，原样返回配置文件值（不过滤）
    if (!request_data.contains("tools") || !request_data["tools"].is_array()
        || request_data["tools"].empty()) {
        return tools_intro;
    }

    // 收集客户端实际传入的工具名集合
    std::unordered_set<std::string> client_tools;
    for (const auto& tool : request_data["tools"]) {
        if (tool.contains("function") && tool["function"].contains("name")) {
            client_tools.insert(tool["function"]["name"].get<std::string>());
        }
    }

    // 逐行过滤：保留非工具行，以及工具名在 client_tools 中的行
    // 工具行格式：以 "- " 开头，后跟 "toolname(" 或 "toolname "
    std::istringstream iss(tools_intro);
    std::ostringstream oss;
    std::string line;
    int removed = 0;
    while (std::getline(iss, line)) {
        if (line.size() >= 2 && line[0] == '-' && line[1] == ' ') {
            // 提取工具名：从 "- " 之后到第一个 '(' 或空格
            std::string rest = line.substr(2);
            auto paren_pos = rest.find('(');
            auto space_pos = rest.find(' ');
            auto end_pos = std::min(paren_pos, space_pos);
            std::string tool_name = (end_pos != std::string::npos) ? rest.substr(0, end_pos) : rest;
            if (client_tools.count(tool_name) == 0) {
                ++removed;
                continue;  // 客户端未传入此工具，跳过
            }
        }
        oss << line << "\n";
    }

    if (removed > 0) {
        My_Log{My_Log::Level::kInfo}
            << "[FilterToolsIntroByRequest] Filtered " << removed
            << " tool(s) not present in client request" << std::endl;
    }

    return oss.str();
}

std::string PromptOptimizer::GetOptimizedToolDefinition(const std::string& tool_name) {
    if (tool_name == "read") {
        return R"JSON({"type":"function","function":{"name":"read","description":"Read the contents of a file. Supports text files and images (jpg, png, gif, webp). Images are sent as attachments. For text files, output is truncated to 2000 lines or 50KB (whichever is hit first). Use offset/limit for large files. When you need the full file, continue with offset until complete.","parameters":{"type":"object","required":["path"],"properties":{"path":{"description":"Path to the file to read (relative or absolute)","type":"string"},"offset":{"description":"Line number to start reading from (1-indexed)","type":"number"},"limit":{"description":"Maximum number of lines to read","type":"number"}}},"strict":false}})JSON";
    } else if (tool_name == "write") {
        return R"JSON({"type":"function","function":{"name":"write","description":"Write content to a file (complete overwrite only). Creates the file if it doesn't exist, replaces entire content if it does. Automatically creates parent directories. Does NOT support offset/limit parameters - always writes the full content. For partial edits use the 'edit' tool instead.","parameters":{"type":"object","required":["path", "content"],"properties":{"path":{"description":"Path to the file to write (relative or absolute)","type":"string"},"content":{"description":"Complete content to write to the file (replaces entire file)","type":"string"}}},"strict":false}})JSON";
    } else if (tool_name == "edit") {
        return R"JSON({"type":"function","function":{"name":"edit","description":"Edit a single file using exact text replacement. Every edits[].oldText must match a unique, non-overlapping region of the original file. If two changes affect the same block or nearby lines, merge them into one edit instead of emitting overlapping edits.","parameters":{"additionalProperties":false,"type":"object","required":["path","edits"],"properties":{"path":{"description":"Path to the file to edit (relative or absolute)","type":"string"},"edits":{"description":"One or more targeted replacements. Each edit is matched against the original file, not incrementally.","type":"array","items":{"additionalProperties":false,"type":"object","required":["oldText","newText"],"properties":{"oldText":{"description":"Exact text for one targeted replacement. Must be unique in the original file.","type":"string"},"newText":{"description":"Replacement text for this targeted edit.","type":"string"}}}}}}}}})JSON";
    } else if (tool_name == "exec") {
        return R"JSON({"type":"function","function":{"name":"exec","description":"Execute commands on Windows system.","parameters":{"type":"object","required":["command"],"properties":{"command":{"description":"Windows command to execute","type":"string"},"timeout":{"description":"Timeout in seconds (kills process on expiry)","type":"number"}}},"strict":false}})JSON";
    } else if (tool_name == "browser") {
        return R"JSON({"type":"function","function":{"name":"browser","description":"Control web browser. Common actions: status (check browser), start (launch browser), open (navigate to URL), snapshot (get page content), screenshot (capture image), act (UI automation with kind parameter).","parameters":{"type":"object","required":["action"],"properties":{"action":{"type":"string","enum":["status","start","stop","open","snapshot","screenshot","navigate","act","close","tabs","focus","console","pdf","upload","dialog","profiles"],"description":"Browser action to perform"},"profile":{"type":"string","description":"Browser profile: 'chrome' (existing Chrome) or 'openclaw' (isolated)"},"target":{"type":"string","enum":["sandbox","host","node"],"description":"Browser location (default: host)"},"targetUrl":{"type":"string","description":"URL to open/navigate"},"targetId":{"type":"string","description":"Tab ID for operations"},"ref":{"type":"string","description":"Element reference from snapshot (e.g., 'e12')"},"text":{"type":"string","description":"Text to type"},"selector":{"type":"string","description":"CSS selector for element"},"kind":{"type":"string","enum":["click","type","press","hover","drag","select","fill","wait","evaluate"],"description":"Interaction type for act action"},"refs":{"type":"string","enum":["role","aria"],"description":"Snapshot reference format (default: role, aria is more stable)"},"fullPage":{"type":"boolean","description":"Capture full page for screenshot"}}},"strict":false}})JSON";
    } else if (tool_name == "cron") {
        return R"JSON({"type":"function","function":{"name":"cron","description":"Manage scheduled tasks and reminders. Actions: list (show jobs), add (create job), remove (delete job), run (trigger now), wake (send reminder).","parameters":{"type":"object","required":["action"],"properties":{"action":{"type":"string","enum":["status","list","add","update","remove","run","wake"],"description":"Cron action"},"job":{"type":"object","description":"Job definition for 'add' action with schedule, payload, sessionTarget"},"jobId":{"type":"string","description":"Job ID for update/remove/run"},"text":{"type":"string","description":"Reminder text for 'wake' action"},"mode":{"type":"string","enum":["now","next-heartbeat"],"description":"Wake timing"}}},"strict":false}})JSON";
    } else if (tool_name == "web_search") {
        return R"JSON({"type":"function","function":{"name":"web_search","description":"Search the web using Brave Search API. Returns titles, URLs, and snippets.","parameters":{"type":"object","required":["query"],"properties":{"query":{"type":"string","description":"Search query"},"count":{"type":"number","description":"Number of results (1-10, default 5)"},"country":{"type":"string","description":"Country code (e.g., 'US', 'DE', 'CN')"},"freshness":{"type":"string","description":"Time filter: 'pd' (24h), 'pw' (week), 'pm' (month), 'py' (year)"}}},"strict":false}})JSON";
    } else if (tool_name == "web_fetch") {
        return R"JSON({"type":"function","function":{"name":"web_fetch","description":"Fetch and extract readable content from a URL. Converts HTML to markdown or plain text.","parameters":{"type":"object","required":["url"],"properties":{"url":{"type":"string","description":"HTTP/HTTPS URL to fetch"},"extractMode":{"type":"string","enum":["markdown","text"],"description":"Extraction format (default: markdown)"},"maxChars":{"type":"number","description":"Maximum characters to return"}}},"strict":false}})JSON";
    } else if (tool_name == "image") {
        return "";
    }

    return "";  // 未知工具,返回空字符串
}

std::vector<std::string> PromptOptimizer::ConvertOpenAIToolCalls(
    const json& tool_calls_array)
{
    std::vector<std::string> converted_calls;
    
    if (!tool_calls_array.is_array()) {
        My_Log{} << "Warning: tool_calls is not an array" << std::endl;
        return converted_calls;
    }

    for (const auto& tool_call : tool_calls_array)
    {
        if (!tool_call.contains("function") || !tool_call["function"].contains("name")) {
            My_Log{} << "Warning: tool_call missing function or name field" << std::endl;
            continue;
        }
        
        std::string tool_name = tool_call["function"]["name"];
        std::string tool_args = tool_call["function"].contains("arguments") ?
                               tool_call["function"]["arguments"].get<std::string>() : "{}";
        
        // 转换为内部格式
        std::string converted = "<tool_call>\n{\"name\": \"" + tool_name +
                               "\", \"arguments\": " + tool_args + "}\n</tool_call>";

        converted_calls.push_back(converted);
        
        My_Log{} << "Converted OpenAI tool_call: " << tool_name << std::endl;
    }
    
    return converted_calls;
}

std::string PromptOptimizer::InjectSpawnGuard(const std::string& tool_response_content) const
{
    const auto& sg_cfg = model_config_.GetPromptOptimizationConfig().spawn_guard;
    if (!sg_cfg.enabled)
        return tool_response_content;

    try
    {
        auto resp_json = json::parse(tool_response_content, nullptr, false);
        if (!resp_json.is_discarded() && resp_json.is_object() &&
            resp_json.contains("childSessionKey") && resp_json.contains("status"))
        {
            const std::string child_key = resp_json.value("childSessionKey", "");
            const std::string status    = resp_json.value("status", "");
            if (status == "accepted" && !child_key.empty())
            {
                // 将 header 中的 {child_key} 占位符替换为实际值
                std::string header = sg_cfg.header;
                const std::string placeholder = "{child_key}";
                auto pos = header.find(placeholder);
                if (pos != std::string::npos)
                    header.replace(pos, placeholder.size(), child_key);

                My_Log{My_Log::Level::kWarning}
                    << "[SpawnGuard] Injected wait directive for childSessionKey="
                    << child_key << std::endl;

                return tool_response_content + "\n\n" + header + "\n" + sg_cfg.body;
            }
        }
    }
    catch (...) { /* 非 JSON 内容，跳过注入 */ }

    return tool_response_content;
}

std::string PromptOptimizer::OptimizeToolResponse(
    const std::string& tool_response)
{
    // 去除首尾空行
    std::string trimmed = tool_response;
    trimmed = std::regex_replace(trimmed, std::regex(R"(^(\s*\n)+)"), "");
    trimmed = std::regex_replace(trimmed, std::regex(R"((\s*\n)+$)"), "");

    // [SpawnGuard] 检测 sessions_spawn 的异步响应，注入强制等待指令
    // 当 tool_response 包含 childSessionKey 字段时，说明这是 sessions_spawn 返回的
    // "status=accepted" 响应。小模型（如 qwen3-4b）容易在收到此响应后误以为任务
    // 尚未执行，从而重复调用 sessions_spawn，导致创建多个重复的 subagent。
    // 通过在 tool_response 内容末尾追加强制等待指令，在 prompt 层面阻断这一行为。
    trimmed = InjectSpawnGuard(trimmed);

    // 包装为工具响应格式
    return "<tool_response>\n" + trimmed + "\n</tool_response>\n";
}

// ========== Harmony 格式专用优化函数实现 ==========

std::string PromptOptimizer::OptimizeHarmonySystemMessage(
    const std::string& knowledge_cutoff,
    const std::string& current_date,
    const std::string& reasoning_level,
    bool has_tools)
{
    // 根据 openai-harmony.md 第 214-240 行的规范构建完整的 system 消息
    // 必须包含：身份、日期、Reasoning 级别、Valid channels 声明、工具 channel 声明
    
    std::string result = "You are ChatGPT, a large language model trained by OpenAI.\n";
    result += "Knowledge cutoff: " + knowledge_cutoff + "\n";
    result += "Current date: " + current_date + "\n\n";
    
    // 添加 Reasoning 级别（必需）
    result += "Reasoning: " + reasoning_level + "\n\n";
    
    // 添加 Valid channels 声明（必需）
    result += "# Valid channels: analysis, commentary, final. Channel must be included for every message.";
    
    // 如果有工具，添加工具 channel 声明
    if (has_tools) {
        result += "\nCalls to these tools must go to the commentary channel: 'functions'.";
    }
    
    My_Log{My_Log::Level::kDebug} << "[Harmony] System message built (" << result.length()
                                   << " bytes, reasoning: " << reasoning_level
                                   << ", tools: " << (has_tools ? "Yes" : "No") << ")" << std::endl;
    
    return result;
}

std::string PromptOptimizer::OptimizeHarmonyDeveloperMessage(
    const std::string& instructions,
    const json& tools,
    const nlohmann::ordered_json& request_data)
{
    // 根据 openai-harmony.md 第 241-257 行的规范构建 developer 消息
    // 包含三部分：
    // 1. System Information（系统信息）
    // 2. Instructions（优化后的指令，仅当 prompt_sections.enabled=true 时）
    // 3. Tools（如果存在，转换为 TypeScript 格式）
    
    std::string result;
    
    // 1. 添加系统上下文（使用统一的 BuildSystemContext 方法）
    result += "# System Context\n\n";
    result += BuildSystemContext(request_data);
    
    size_t system_context_tokens = CountTokens(result);
    
    // 2. 根据 prompt_sections.enabled 决定是否输出原始 instructions 内容
    // enabled=false：完全不输出原始提示词内容，仅使用 BuildSystemContext 的输出
    // enabled=true ：将原始 instructions 压缩后追加，并按 rules 过滤额外段落
    size_t instructions_tokens = 0;
    const PromptSectionsConfig& sections_cfg = model_config_.GetPromptSectionsConfig();
    if (sections_cfg.enabled) {
        result += "# Instructions\n\n";

        // 优化 instructions
        std::string optimized_instructions = OptimizeInstructions(instructions);
        result += optimized_instructions;

        instructions_tokens = CountTokens(optimized_instructions);

        // 根据 prompt_sections 配置，从原始 instructions 中提取额外段落并追加
        std::string filtered_sections = AppendFilteredSections(instructions);
        if (!filtered_sections.empty()) {
            result += "\n\n# Additional Context\n\n";
            result += filtered_sections;
            My_Log{My_Log::Level::kDebug} << "[Harmony] Appended filtered sections ("
                                           << filtered_sections.length() << " bytes)" << std::endl;
        }
    } else {
        My_Log{My_Log::Level::kDebug} << "[Harmony] prompt_sections disabled, skipping original instructions" << std::endl;
    }

    // 4. 如果有工具，添加工具定义和使用指导
    if (!tools.is_null() && !tools.empty()) {
        // 从 system_context 配置中读取 "Tool Usage Guidelines" 段落
        const SystemContextConfig& ctx_cfg = model_config_.GetSystemContextConfig();

        for (const auto& sec : ctx_cfg.sections) {
            if (!sec.enabled) continue;
            if (sec.title.find("Tool Usage Guidelines") != std::string::npos) {
                result += "\n\n" + sec.title + "\n\n";
                for (const auto& line : sec.lines) {
                    result += line + "\n";
                }
                My_Log{My_Log::Level::kDebug} << "[Harmony] Tool Usage Guidelines loaded from system_context config" << std::endl;
                break;
            }
        }
        
        std::string tools_section = "\n# Tools\n\n## functions\n\n";
        tools_section += "namespace functions {\n\n";
        
        std::string ts_tools = ConvertToolsToOptimizedTypeScript(tools);
        tools_section += ts_tools;
        
        tools_section += "\n} // namespace functions";
        
        result += tools_section;
        
        size_t tools_tokens = CountTokens(ts_tools);
        
        My_Log{My_Log::Level::kDebug} << "[Harmony] Developer message - System: " << system_context_tokens
                                       << " tokens, Instructions: " << instructions_tokens
                                       << " tokens, Tools: " << tools_tokens
                                       << " tokens, Total: " << (system_context_tokens + instructions_tokens + tools_tokens) << std::endl;
    } else {
        My_Log{My_Log::Level::kDebug} << "[Harmony] Developer message - System: " << system_context_tokens
                                       << " tokens, Instructions: " << instructions_tokens
                                       << " tokens, Total: " << (system_context_tokens + instructions_tokens)
                                       << " tokens (no tools)" << std::endl;
    }
    
    return result;
}

std::string PromptOptimizer::AppendFilteredSections(const std::string& source_prompt)
{
    // 从 source_prompt 中按 prompt_sections 配置过滤段落，返回应追加的内容字符串
    // disabled 或无匹配段落时返回空串
    const PromptSectionsConfig& sections_cfg = model_config_.GetPromptSectionsConfig();
    if (!sections_cfg.enabled) {
        My_Log{My_Log::Level::kDebug} << "[SectionFilter] prompt_sections disabled, skipping" << std::endl;
        return "";
    }
    std::vector<PromptSection> sections = ParseMarkdownSections(source_prompt);
    std::string filtered = FilterSectionsByConfig(sections, sections_cfg);
    if (!filtered.empty()) {
        My_Log{My_Log::Level::kDebug} << "[SectionFilter] AppendFilteredSections: "
                                       << filtered.length() << " bytes appended" << std::endl;
    }
    return filtered;
}

std::string PromptOptimizer::ConvertToolsToOptimizedTypeScript(const json& tools)
{
    // 将 OpenAI JSON 格式的工具定义转换为精简的 TypeScript 格式
    // 根据 openai-harmony.md 第 319-371 行的规范
    
    std::string result;
    size_t tool_count = 0;
    
    if (!tools.is_array()) {
        My_Log{My_Log::Level::kInfo} << "Tools is not an array, skipping conversion" << std::endl;
        return result;
    }
    
    for (const auto& tool : tools) {
        if (!tool.contains("function") || !tool["function"].contains("name")) {
            My_Log{My_Log::Level::kInfo} << "Tool missing function or name field, skipping" << std::endl;
            continue;
        }
        
        std::string tool_name = tool["function"]["name"];
        
        // 尝试获取预定义的优化定义
        std::string ts_def = GetOptimizedTypeScriptDefinition(tool_name);
        
        if (!ts_def.empty()) {
            result += ts_def + "\n\n";
            tool_count++;
            My_Log{My_Log::Level::kDebug} << "[Harmony] Using optimized TypeScript definition for: " << tool_name << std::endl;
        } else {
            // 对于未知工具，生成基本定义
            std::string basic_def = GenerateBasicTypeScriptDefinition(tool);
            if (!basic_def.empty()) {
                result += basic_def + "\n\n";
                tool_count++;
                My_Log{My_Log::Level::kDebug} << "[Harmony] Generated basic TypeScript definition for: " << tool_name << std::endl;
            }
        }
    }
    
    My_Log{My_Log::Level::kDebug} << "[Harmony] Converted " << tool_count << " tools to TypeScript format" << std::endl;
    
    return result;
}

std::string PromptOptimizer::OptimizeInstructions(const std::string& instructions)
{
    // 优化 instructions 部分
    // 策略：
    // 1. 若包含 SKILL 部分，提取并使用优化后的 SKILL 格式
    // 2. 否则，使用基于段落的摘要策略：
    //    - 始终保留所有标题行（# 开头）
    //    - 保留每个段落的第一句话（段落摘要）
    //    - 若总长度仍超过阈值，对超长段落进一步截断
    //    原因：原来的关键词过滤（must/should/always/never 等）会丢失 JSON 格式要求、
    //    角色定义等不含关键词但同样重要的上下文信息。
    //    基于段落的摘要策略保留了每个段落的核心语义，同时大幅减少冗余描述。

    std::string result;

    // 基础清理：压缩多余空行
    result = std::regex_replace(instructions, std::regex(R"(\n{3,})"), "\n\n");

    // 若长度未超过阈值，直接返回（无需摘要）
    static const size_t kSummaryThreshold = 500;
    if (result.length() <= kSummaryThreshold) {
        return result;
    }

    // ── 基于段落的摘要策略 ──────────────────────────────────────────────────
    // 将文本按空行分割为段落，对每个段落：
    //   - 若段落以 '#' 开头（标题行），完整保留
    //   - 否则只保留段落的第一句话（以 '.', '!', '?' 或换行结尾）
    // 这样可以保留所有段落的核心语义，同时大幅减少冗余描述。

    std::string summarized;
    summarized.reserve(result.size() / 2);  // 预估压缩后约为原来的一半

    std::istringstream iss(result);
    std::string line;
    std::string current_paragraph;

    // 辅助 Lambda：处理并输出一个完整段落
    auto flush_paragraph = [&](const std::string& para) {
        if (para.empty()) return;

        // 标题行（以 '#' 开头）：完整保留
        if (para[0] == '#') {
            summarized += para + "\n\n";
            return;
        }

        // [Opt 建议4] 列表段落（以 '-'、'*'、数字+'.' 开头）：完整保留
        // 列表项通常是格式约束、枚举规则、JSON 格式要求等关键信息，
        // 只取第一行会丢失后续列表项，导致模型缺失关键约束。
        // 检测规则：段落第一行以 '-'、'*' 或 "数字." 开头（如 "1. "、"2. "）
        {
            // 取段落第一行（到第一个 '\n' 为止）
            size_t first_line_end = para.find('\n');
            const std::string& first_line = (first_line_end != std::string::npos)
                                            ? para.substr(0, first_line_end)
                                            : para;
            // 跳过前导空格
            size_t non_space = first_line.find_first_not_of(" \t");
            if (non_space != std::string::npos) {
                char c0 = first_line[non_space];
                bool is_list = (c0 == '-' || c0 == '*');
                // 检测 "数字." 格式（如 "1. "、"10. "）
                if (!is_list && std::isdigit(static_cast<unsigned char>(c0))) {
                    size_t dot_pos = first_line.find('.', non_space + 1);
                    if (dot_pos != std::string::npos &&
                        dot_pos == non_space + (first_line.find_first_not_of("0123456789", non_space) - non_space)) {
                        is_list = true;
                    }
                }
                if (is_list) {
                    summarized += para + "\n\n";
                    return;
                }
            }
        }

        // 普通段落：只保留第一句话
        // 第一句话定义：到第一个句末标点（. ! ?）或第一个换行符为止
        size_t first_sentence_end = std::string::npos;
        for (size_t k = 0; k < para.size(); k++) {
            char c = para[k];
            if (c == '.' || c == '!' || c == '?') {
                first_sentence_end = k + 1;  // 包含标点本身
                break;
            }
            if (c == '\n') {
                first_sentence_end = k;
                break;
            }
        }

        if (first_sentence_end != std::string::npos && first_sentence_end < para.size()) {
            // 有多句话：只保留第一句
            // 使用 UTF-8 安全截断，确保不会在多字节字符中间截断
            // （first_sentence_end 是按字节计算的位置，可能落在多字节字符中间）
            summarized += safe_utf8_truncate(para, first_sentence_end, "") + "\n\n";
        } else {
            // 只有一句话或无标点：完整保留
            summarized += para + "\n\n";
        }
    };

    while (std::getline(iss, line)) {
        if (line.empty()) {
            // 空行：段落分隔符，处理当前段落
            flush_paragraph(current_paragraph);
            current_paragraph.clear();
        } else {
            if (!current_paragraph.empty()) current_paragraph += '\n';
            current_paragraph += line;
        }
    }
    // 处理最后一个段落（文件末尾无空行时）
    flush_paragraph(current_paragraph);

    // 清理末尾多余空行
    while (summarized.size() >= 2 &&
           summarized[summarized.size()-1] == '\n' &&
           summarized[summarized.size()-2] == '\n') {
        summarized.pop_back();
    }

    My_Log{My_Log::Level::kDebug} << "[Optimizer] Paragraph summary: reduced instructions from "
                                   << instructions.length() << " to " << summarized.length() << " bytes" << std::endl;
    return summarized;
}

std::string PromptOptimizer::GetOptimizedTypeScriptDefinition(const std::string& tool_name)
{
    // 返回预定义的精简 TypeScript 工具定义
    // 根据 openai-harmony.md 第 319-371 行的格式
    // 优化版：使用中文注释，更易理解
    
    if (tool_name == "read") {
        return R"(// Read file contents (supports text and images: jpg/png/gif/webp)
// Use offset/limit for chunked reading of large files
type read = (_: {
  path: string,
  offset?: number,      // starting line number (1-based)
  limit?: number,       // maximum number of lines to read
}) => any;)";
    } else if (tool_name == "write") {
        return R"(// Write file (full overwrite; use edit for partial changes)
type write = (_: {
  path: string,
  content: string,
}) => any;)";
    } else if (tool_name == "edit") {
        return R"(// Edit a file with one or more exact text replacements (each oldText must be unique in the file)
type edit = (_: {
  path: string,
  edits: Array<{
    oldText: string,  // exact text to find (must be unique in the file)
    newText: string,  // replacement text
  }>,
}) => any;)";
    } else if (tool_name == "exec") {
        return R"(// Execute a Windows command (use with caution)
type exec = (_: {
  command: string,
  timeout?: number,     // timeout in seconds; process is killed on expiry
}) => any;)";
    } else if (tool_name == "browser") {
        return R"(// Control a browser (open pages, take screenshots, interact with UI)
type browser = (_: {
  action: "status" | "start" | "stop" | "open" | "snapshot" | "screenshot" | "navigate" | "act" | "close" | "tabs" | "focus" | "console" | "pdf" | "upload" | "dialog" | "profiles",
  profile?: string,     // browser profile: "chrome" (existing Chrome) or "openclaw" (isolated browser)
  target?: "sandbox" | "host" | "node",  // browser location (default: "host")
  targetUrl?: string,
  targetId?: string,    // tab ID
  ref?: string,         // element reference from snapshot (e.g. "e12")
  text?: string,
  selector?: string,
  kind?: "click" | "type" | "press" | "hover" | "drag" | "select" | "fill" | "wait" | "evaluate",
                        // interaction type for the "act" action
  refs?: "role" | "aria",  // snapshot reference format (default: "role"; "aria" is more stable)
  fullPage?: boolean,   // whether to capture the full page for screenshot
}) => any;)";
    } else if (tool_name == "cron") {
        return R"(// Manage scheduled tasks and reminders
type cron = (_: {
  action: string,       // operation: "status" | "list" | "add" | "update" | "remove" | "run" | "wake"
  job?: any,            // task definition for "add" (contains schedule, payload, sessionTarget)
  jobId?: string,       // task ID for "update" / "remove" / "run"
  text?: string,
  mode?: string,        // trigger timing: "now" (immediate) | "next-heartbeat"
}) => any;)";
    } else if (tool_name == "web_search") {
        return R"(// Search the web. Use this tool to look up any information. Results include titles, URLs and snippets.
// When results contain web pages, use "web_fetch" to retrieve the full content.
type web_search = (_: {
  query: string,
  count?: number,       // number of results (1-10, default 5)
}) => any;)";
    } else if (tool_name == "web_fetch") {
        return R"(// Fetch web page content (HTML converted to markdown or plain text)
// Lightweight web access without browser automation
type web_fetch = (_: {
  url: string,
  extractMode?: string, // output format: "markdown" (default) or "text"
  maxChars?: number,    // maximum characters to return; content is truncated if exceeded
}) => any;)";
    } else if (tool_name == "image") {
        // filter out the image tool
        My_Log{My_Log::Level::kDebug} << "[Harmony] Filtered out 'image' tool" << std::endl;
        return "";
    }
    
    return "";  // unknown tool, return empty string
}

std::string PromptOptimizer::GenerateBasicTypeScriptDefinition(const json& tool)
{
    // 为未知工具生成基本的 TypeScript 定义
    // 根据 openai-harmony.md 第 319-371 行的格式
    
    try {
        std::string tool_name = tool["function"]["name"];
        std::string description = tool["function"].value("description", "");
        
        // 过滤掉 image 工具
        if (tool_name == "image") {
            return "";
        }
        
        std::string result;
        
        // 添加描述（如果存在，移除长度限制以确保完整输出）
        if (!description.empty()) {
            result += "// " + description + "\n";
        }
        
        // 检查是否有参数
        if (tool["function"].contains("parameters") &&
            tool["function"]["parameters"].contains("properties") &&
            !tool["function"]["parameters"]["properties"].empty())
        {
            // 有参数的函数
            result += "type " + tool_name + " = (_: {\n";
            
            const auto& properties = tool["function"]["parameters"]["properties"];
            const auto& required = tool["function"]["parameters"].value("required", json::array());
            
            for (auto it = properties.begin(); it != properties.end(); ++it) {
                std::string param_name = it.key();
                const auto& param_def = it.value();
                
                // 检查是否是必需参数
                bool is_required = std::find(required.begin(), required.end(), param_name) != required.end();
                
                // 获取参数类型：优先检查 enum 字段，生成联合类型
                std::string param_type;
                if (param_def.contains("enum") && param_def["enum"].is_array() && !param_def["enum"].empty()) {
                    std::string union_type;
                    for (const auto& val : param_def["enum"]) {
                        if (!union_type.empty()) union_type += " | ";
                        if (val.is_string()) {
                            union_type += "\"" + val.get<std::string>() + "\"";
                        } else {
                            union_type += val.dump();
                        }
                    }
                    param_type = union_type;
                } else {
                    param_type = param_def.value("type", "any");
                    if (param_type == "integer" || param_type == "number") {
                        param_type = "number";
                    } else if (param_type == "boolean") {
                        param_type = "boolean";
                    } else if (param_type == "array") {
                        param_type = "any[]";
                    } else if (param_type == "object") {
                        param_type = "any";
                    } else {
                        param_type = "string";
                    }
                }
                
                // 获取参数描述
                std::string param_desc = param_def.value("description", "");
                
                result += "  " + param_name;
                if (!is_required) {
                    result += "?";
                }
                result += ": " + param_type + ",";
                
                // 追加参数描述注释（截断超过60字符的描述）
                if (!param_desc.empty()) {
                    if (param_desc.length() > 60) {
                        // 使用 UTF-8 安全截断，避免在多字节字符（如中文）中间截断
                        param_desc = safe_utf8_truncate(param_desc, 60, "...");
                    }
                    result += "    // " + param_desc;
                }
                result += "\n";
            }
            
            result += "}) => any;";
        }
        else
        {
            // 无参数的函数
            result += "type " + tool_name + " = () => any;";
        }
        
        return result;
        
    } catch (const std::exception& e) {
        My_Log{My_Log::Level::kError}
            << "Failed to generate TypeScript definition: " << e.what() << std::endl;
        return "";
    }
}

// ========== 原始提示词段落过滤实现 ==========

std::string PromptOptimizer::ToLower(const std::string& s)
{
    std::string result = s;
    std::transform(result.begin(), result.end(), result.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return result;
}

std::vector<PromptOptimizer::PromptSection> PromptOptimizer::ParseMarkdownSections(
    const std::string& prompt)
{
    // 将原始提示词按 Markdown 标题行（# / ## / ###）分割成段落列表
    // 每个段落包含：标题级别、标题文本、完整内容（含标题行）
    //
    // 算法：
    //   1. 逐行扫描，遇到标题行时开始新段落
    //   2. 前一个段落的内容到下一个标题行（或文件末尾）为止
    //   3. 文件开头到第一个标题行之前的内容作为 heading_level=0 的段落（前言）

    std::vector<PromptSection> sections;

    std::istringstream iss(prompt);
    std::string line;

    // 当前段落的累积内容
    std::string current_content;
    int current_level = 0;
    std::string current_title;

    auto flush_section = [&]() {
        if (current_level == 0 && current_content.empty()) return;
        // 去除尾部多余空行
        while (current_content.size() >= 1 &&
               current_content.back() == '\n') {
            current_content.pop_back();
        }
        if (!current_content.empty() || current_level > 0) {
            PromptSection sec;
            sec.heading_level = current_level;
            sec.title = current_title;
            sec.full_content = current_content;
            sections.push_back(sec);
        }
    };

    while (std::getline(iss, line)) {
        // 检测标题行：以 1-3 个 '#' 开头，后跟空格
        int level = 0;
        if (!line.empty() && line[0] == '#') {
            size_t i = 0;
            while (i < line.size() && line[i] == '#') { ++i; }
            if (i <= 3 && i < line.size() && line[i] == ' ') {
                level = static_cast<int>(i);
            }
        }

        if (level > 0) {
            // 遇到新标题：先保存当前段落
            flush_section();

            // 开始新段落
            current_level = level;
            // 提取标题文本（去除前导 '#' 和空格）
            current_title = line.substr(level + 1); // 跳过 "### " 中的 '#' 和空格
            // 去除标题文本前后空白
            size_t ts = current_title.find_first_not_of(" \t");
            size_t te = current_title.find_last_not_of(" \t\r");
            if (ts != std::string::npos) {
                current_title = current_title.substr(ts, te - ts + 1);
            } else {
                current_title.clear();
            }
            current_content = line + "\n";
        } else {
            // 普通行：追加到当前段落
            current_content += line + "\n";
        }
    }

    // 保存最后一个段落
    flush_section();

    My_Log{My_Log::Level::kDebug} << "[SectionFilter] Parsed " << sections.size()
                                   << " sections from prompt (" << prompt.size() << " bytes)" << std::endl;
    return sections;
}

bool PromptOptimizer::ShouldIncludeSection(
    const PromptSection& section,
    const PromptSectionsConfig& config)
{
    // 对每条规则按顺序匹配，第一个命中的规则生效
    std::string title_lower = ToLower(section.title);

    for (const auto& rule : config.rules) {
        // 检查 heading_level（0=任意级别）
        if (rule.heading_level != 0 && rule.heading_level != section.heading_level) {
            continue;
        }
        // 检查 title_contains（大小写不敏感子串匹配）
        if (!rule.title_contains.empty()) {
            std::string keyword_lower = ToLower(rule.title_contains);
            if (title_lower.find(keyword_lower) == std::string::npos) {
                continue;
            }
        }
        // 命中规则
        My_Log{My_Log::Level::kDebug} << "[SectionFilter] Rule matched: title='" << section.title
                                       << "' (level=" << section.heading_level
                                       << ") -> " << (rule.include ? "include" : "exclude") << std::endl;
        return rule.include;
    }

    // 未命中任何规则，使用 default_action
    bool default_include = (config.default_action == "include");
    My_Log{My_Log::Level::kDebug} << "[SectionFilter] No rule matched: title='" << section.title
                                   << "' (level=" << section.heading_level
                                   << ") -> default_action=" << config.default_action << std::endl;
    return default_include;
}

std::string PromptOptimizer::FilterSectionsByConfig(
    const std::vector<PromptSection>& sections,
    const PromptSectionsConfig& config)
{
    if (sections.empty()) return "";

    std::string result;
    int included_count = 0;
    int excluded_count = 0;

    for (const auto& section : sections) {
        // heading_level=0 的前言段落（文件头部无标题内容）：
        // 通常是 "You are a personal assistant..." 这类核心身份描述，
        // 但在 OptimizeSystemPrompt 中我们已经用 BuildSystemContext() 替换了，
        // 所以这里跳过 level=0 的段落，避免重复
        if (section.heading_level == 0) {
            My_Log{My_Log::Level::kDebug} << "[SectionFilter] Skipping preamble section (level=0)" << std::endl;
            continue;
        }

        bool should_include = ShouldIncludeSection(section, config);

        if (!should_include) {
            ++excluded_count;
            continue;
        }

        // 应保留该段落
        std::string content = section.full_content;

        // 如果配置了 max_section_tokens，截断超长段落
        if (config.max_section_tokens > 0) {
            size_t token_count = CountTokens(content);
            if (token_count > static_cast<size_t>(config.max_section_tokens)) {
                // 按比例估算截断字符数（粗略：1 token ≈ 4 chars）
                size_t max_chars = static_cast<size_t>(config.max_section_tokens) * 4;
                if (content.size() > max_chars) {
                    // 使用 UTF-8 安全截断，避免在多字节字符（如中文）中间截断
                    // 原来的 content.substr(0, max_chars) 是纯字节截断，
                    // 当 max_chars 落在多字节字符中间时会产生无效 UTF-8 序列，
                    // 导致 Rust tokenizer 在解析时 panic（Utf8Error）
                    content = safe_utf8_truncate(content, max_chars, "\n...[truncated]\n");
                    My_Log{My_Log::Level::kDebug} << "[SectionFilter] Section '" << section.title
                                                   << "' truncated to " << max_chars << " chars"
                                                   << " (max_section_tokens=" << config.max_section_tokens << ")" << std::endl;
                }
            }
        }

        result += content;
        if (!result.empty() && result.back() != '\n') result += '\n';
        result += '\n';  // 段落间空行
        ++included_count;
    }

    My_Log{My_Log::Level::kInfo} << "[SectionFilter] Filtered sections: included=" << included_count
                                  << ", excluded=" << excluded_count
                                  << ", output_size=" << result.size() << " bytes" << std::endl;
    return result;
}
