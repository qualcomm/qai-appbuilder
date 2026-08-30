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
#include <cstdint>
#include <sstream>
#include <ctime>
#include <regex>
#include <unordered_map>
#include <unordered_set>
#include <utility>
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

        // ── Step 1: 模板重建（与 MainAgent 的 OptimizeSystemPrompt 完全相同）────────
        // 调用 BuildSystemContext() 重建核心骨架：
        //   identity_intro + skill_rule(有SKILL时) + tools_intro + skill_catalog + few_shot_examples
        // 这与 MainAgent 的处理完全一致，复用同一套配置驱动的模板逻辑。
        // BuildSystemContext() 内部会调用 ExtractSkillsFromRequest() 解析 Skills，
        // 并调用 SetRuntimeSkillMappings() 写入运行时映射（供 AutoCorrectSkillCall 使用）。
        // 同时接收本轮相关性筛选得到的意图判定结果，用于填充 OptimizationStats。
        IntentType detected_intent = IntentType::GENERAL_CHAT;
        std::string matched_skill;
        std::string optimized = BuildSystemContext(request_data, &detected_intent, &matched_skill);
        last_stats_.detected_intent = detected_intent;
        last_stats_.matched_skill = matched_skill;

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
        // 同时接收本轮相关性筛选得到的意图判定结果，用于填充 OptimizationStats
        IntentType detected_intent = IntentType::GENERAL_CHAT;
        std::string matched_skill;
        std::string optimized = BuildSystemContext(request_data, &detected_intent, &matched_skill);

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
        last_stats_.detected_intent = detected_intent;
        last_stats_.matched_skill = matched_skill;

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

std::string PromptOptimizer::BuildSystemContext(const nlohmann::ordered_json& request_data,
                                                 IntentType* out_intent,
                                                 std::string* out_matched_skill)
{
    std::ostringstream oss;

    const auto& config = model_config_.GetPromptOptimizationConfig();
    const auto& se = config.system_prompts.sections_enabled;

    // 0. 提前解析 runtime_skills（全量），供后续各段落条件判断使用
    // （原位于步骤 5，提前至此以便 skill_rule 能根据是否有 SKILL 决定是否输出）
    RuntimeSkillMappings runtime_skills = ExtractSkillsFromRequest(request_data);

    // 0b. 相关性筛选：只影响"展示给模型看哪些 SKILL"，绝不影响下方步骤 5 & 6 之前
    // SetRuntimeSkillMappings() 写入安全网时使用的 runtime_skills（必须是全量）。
    // relevance_filter.enabled=false 时 filtered_skills 直接等于 runtime_skills，
    // 完全回退到改动前的"全量携带"行为，便于线上快速回滚。
    const auto& relevance_cfg = config.relevance_filter;
    const auto& disclosure_cfg = config.skill_disclosure;
    std::vector<std::string> relevance_keywords;
    RuntimeSkillMappings filtered_skills = runtime_skills;
    // D2：三档渐进披露的档位分配结果（只在 structured 目录格式下使用）。
    std::vector<ScoredSkill> leveled_skills;
    size_t skills_budget_tokens = ComputeRelevanceTokenBudget(BudgetPartitionKind::kSkills, &request_data);
    // D2 仅对默认的 structured 格式生效；"simple" 本身已是单行渲染，
    // 不引入第二套档位口径。skill_disclosure.enabled=false 时逐字节回退到
    // 旧两档行为（FilterSkillsByRelevance + 全 L2 渲染）。
    const bool use_leveled_disclosure =
        disclosure_cfg.enabled && config.skill_catalog_format == "structured";
    if (relevance_cfg.enabled) {
        relevance_keywords = BuildRelevanceKeywords(request_data);
    }
    if (!runtime_skills.empty()) {
        if (use_leveled_disclosure) {
            // 注：relevance_cfg.enabled=false 时 relevance_keywords 为空，
            // AssignSkillDetailLevels 与 FilterSkillsByRelevance 同样不做任何筛选（全保留），
            // 但仍会按预算降档——这正是 D2 要解决的「全量保留在小 context 模型上
            // 等于必然溢出」，与相关性开关解耦。
            leveled_skills = AssignSkillDetailLevels(runtime_skills, relevance_keywords,
                                                     skills_budget_tokens);
            filtered_skills.clear();
            for (const auto& scored : leveled_skills) {
                auto it = runtime_skills.find(scored.name);
                if (it != runtime_skills.end()) {
                    filtered_skills[scored.name] = it->second;
                }
            }
        } else if (relevance_cfg.enabled) {
            filtered_skills = FilterSkillsByRelevance(runtime_skills, relevance_keywords,
                                                       skills_budget_tokens);
        }
    }
    // 供 PromptLedger 可观测性回报使用：不新增第二套统计，直接写入 last_stats_
    // （与 OptimizeToolsPrompt 写入的 tools_tier/total/kept 互不覆盖）
    last_stats_.skills_total = runtime_skills.size();
    last_stats_.skills_kept = filtered_skills.size();
    last_stats_.skills_budget_tokens = skills_budget_tokens;
    // D2：三档计数，三项之和恒等于 skills_kept。未启用三档时逐字节退化为
    // skills_l2 == skills_kept、L1/L0 为 0（旧两档行为：要么全 L2，要么整条删）。
    last_stats_.skills_l1 = 0;
    last_stats_.skills_l0 = 0;
    if (use_leveled_disclosure) {
        size_t l2 = 0, l1 = 0, l0 = 0;
        for (const auto& scored : leveled_skills) {
            if (scored.level == SkillDetailLevel::kFull) ++l2;
            else if (scored.level == SkillDetailLevel::kSummary) ++l1;
            else ++l0;
        }
        last_stats_.skills_l2 = l2;
        last_stats_.skills_l1 = l1;
        last_stats_.skills_l0 = l0;
    } else {
        last_stats_.skills_l2 = filtered_skills.size();
    }

    // 1a. 身份声明（始终输出，与是否有 SKILL 无关）
    if (se.identity_intro && !config.system_prompts.identity_intro.empty()) {
        oss << config.system_prompts.identity_intro;
    }

    // 1b & 2. Skill 规则 + 工具列表（仅在筛选后仍有 SKILL 时输出）
    // 原因：tools_intro 的作用是配合 skill_rule 告知模型"哪些是工具、哪些是 Skill"，
    // 当没有 SKILL 时，这两段提示均无意义，省略可减少 token 消耗并避免引入不存在的概念。
    // 改用 filtered_skills 判断：全部未命中时（零命中兜底）skill_rule/tools_intro
    // 与下方 Skill Catalog/Few-shot 一并清空，与既有"仅在有 SKILL 时才输出"的分支
    // 逻辑天然衔接，无需新增 if 分支。
    if (!filtered_skills.empty()) {
        // 1b. Skill 与 Tool 区分规则
        if (se.skill_rule && !config.system_prompts.skill_rule.empty()) {
            oss << config.system_prompts.skill_rule;
        }

        // 2. 工具列表
        // 优先级：
        //   a. 配置文件有 tools_intro → 使用配置文件值，但过滤掉客户端未传入的工具行
        //   b. 配置文件无 tools_intro → 根据客户端 tools 数组动态生成
        // FilterToolsIntroByRequest()/BuildDynamicToolsIntro() 内部会自行套用
        // FilterToolsByRelevance() 缩小候选工具集合，此处无需重复处理。
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
    // runtime_skills（全量）已在步骤 0 提前解析，filtered_skills（筛选后子集）已在步骤 0b 计算

    // 将运行时 SKILL 映射（name->path）写入 model_config_，
    // 供 ResponseDispatcher::AutoCorrectSkillCall() 在推理完成后读取，
    // 以纠正模型错误地将 SKILL 名当作工具直接调用的情况。
    // ★ 安全网边界：此处必须使用未经相关性过滤的全量 runtime_skills，不能改成
    // filtered_skills——过滤只影响"展示给模型看什么"，不影响"推理完成后能不能被
    // 纠偏"；若误用 filtered_skills，模型调用了未展示的 SKILL 名时将无法被纠正。
    if (!runtime_skills.empty()) {
        SkillMappings name_to_path;
        for (const auto& [name, info] : runtime_skills) {
            if (!info.path.empty()) {
                name_to_path[name] = info.path;
            }
        }
        model_config_.SetRuntimeSkillMappings(name_to_path);
    }

    if (!filtered_skills.empty()) {
        // 5. Skill Catalog（用筛选后的子集构建，展示给模型的候选缩小到本轮相关的部分）
        // catalog_structured_intro 的开关在 BuildStructuredSkillCatalog 内部读取，
        // 但 Skill Catalog 的条目列表（路径/描述）始终输出（仅头部说明受开关控制）
        const auto& opt_config = model_config_.GetPromptOptimizationConfig();
        std::string skill_section;
        if (use_leveled_disclosure && !leveled_skills.empty()) {
            // D2：按档位渲染（全部为 L2 时与旧输出逐字节相同，仅排序改为按分数）
            skill_section = BuildLeveledSkillCatalog(leveled_skills);
        } else if (opt_config.skill_catalog_format == "structured") {
            skill_section = BuildStructuredSkillCatalog(filtered_skills);
        } else {
            skill_section = BuildSimpleSkillCatalog(filtered_skills);
        }
        if (!skill_section.empty()) {
            oss << skill_section;
        }

        // 6. Few-shot 示例（动态生成，受 few_shot_examples_enabled.enabled 总开关控制；
        // 同样基于筛选后的子集生成，避免示例引用未展示的 SKILL）
        if (opt_config.system_prompts.few_shot_examples_enabled.enabled) {
            std::string few_shot = BuildFewShotExamples(filtered_skills);
            if (!few_shot.empty()) {
                oss << few_shot;
            }
        } else {
            My_Log{My_Log::Level::kInfo} << "[BuildSystemContext] few_shot_examples_enabled.enabled=false, skipping few-shot examples" << std::endl;
        }
    }

    // 意图判定（供 OptimizationStats::detected_intent/matched_skill 使用，仅用于
    // 日志/回归排查，不影响上方任何提示词构建逻辑）：
    // - 筛选后仍有命中的 SKILL → SKILL_QUERY，matched_skill 取其中一个命中的 SKILL 名
    //   （用于问题排查，不保证是分数最高者，命中集合通常很小，代表性足够）
    // - 否则若存在相关性命中的工具 → TOOL_CALL
    // - 否则 → GENERAL_CHAT
    if (out_intent) {
        if (!filtered_skills.empty()) {
            *out_intent = IntentType::SKILL_QUERY;
            if (out_matched_skill) {
                *out_matched_skill = filtered_skills.begin()->first;
            }
        } else {
            bool tool_hit = false;
            if (request_data.contains("tools") && request_data["tools"].is_array() && !request_data["tools"].empty()) {
                if (relevance_cfg.enabled) {
                    nlohmann::ordered_json relevant_tools =
                        FilterToolsByRelevance(request_data["tools"], relevance_keywords,
                                               ComputeRelevanceTokenBudget(BudgetPartitionKind::kTools, &request_data));
                    tool_hit = !relevant_tools.empty();
                } else {
                    // 相关性过滤总开关关闭时，无法判断"是否相关"，客户端传了 tools
                    // 即视为本轮可能用到工具，与改动前"全量携带"的语义保持一致
                    tool_hit = true;
                }
            }
            *out_intent = tool_hit ? IntentType::TOOL_CALL : IntentType::GENERAL_CHAT;
            if (out_matched_skill) {
                out_matched_skill->clear();
            }
        }
    }

    return oss.str();
}

size_t PromptOptimizer::CountTokens(const std::string& text) const {
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

// ========== 相关性打分与预算贪心筛选实现 ==========
//
// 设备侧上下文极为有限（如 Omni 模型仅 2048 token），全量携带客户端传入的全部
// SKILL/工具定义是模型第一轮就容易溢出/回答质量下降的直接原因。以下方法用纯
// 字符串/规则匹配（不引入向量/embedding 语义匹配，零额外推理延迟）判定"本轮
// 问题是否真的用得上某个 skill/tool"：命中才保留，未命中整条删除。

namespace {

// 解码 UTF-8 字符串在 pos 处的一个 code point，返回 {code point, 字节长度}。
// 非法/截断的多字节序列按单字节处理，避免越界读取。
std::pair<uint32_t, size_t> DecodeUtf8CodePoint(const std::string& text, size_t pos) {
    unsigned char c = static_cast<unsigned char>(text[pos]);
    if (c < 0x80) {
        return {c, 1};
    }

    size_t len = 0;
    uint32_t cp = 0;
    if ((c & 0xE0) == 0xC0) { len = 2; cp = c & 0x1F; }
    else if ((c & 0xF0) == 0xE0) { len = 3; cp = c & 0x0F; }
    else if ((c & 0xF8) == 0xF0) { len = 4; cp = c & 0x07; }
    else { return {c, 1}; } // 非法首字节，按单字节处理

    if (pos + len > text.size()) {
        return {c, 1};
    }
    for (size_t i = 1; i < len; ++i) {
        unsigned char cc = static_cast<unsigned char>(text[pos + i]);
        if ((cc & 0xC0) != 0x80) {
            return {c, 1}; // 续字节不合法，按单字节处理
        }
        cp = (cp << 6) | (cc & 0x3F);
    }
    return {cp, len};
}

// 判断 code point 是否属于常见的中文/全角标点或空白区间（作为切词边界）：
//   U+2000-U+206F 通用标点（引号/破折号/省略号等）
//   U+3000-U+303F CJK 符号和标点（含全角空格、书名号、顿号等）
//   U+FF00-U+FFEF 半角/全角形式（含全角句号/逗号/括号等）
bool IsCjkPunctuationOrSpace(uint32_t cp) {
    return (cp >= 0x2000 && cp <= 0x206F) ||
           (cp >= 0x3000 && cp <= 0x303F) ||
           (cp >= 0xFF00 && cp <= 0xFFEF);
}

// 判断一个关键词是否达到参与相关性匹配的最小门槛：纯 ASCII 字符组成的关键词
// 长度必须 >= 3，避免 "s"/"i"/"a" 这类英文缩写残留/停用词几乎命中一切 skill/tool
// 名称或描述，从而破坏"零命中即整段清空"的兜底策略；含非 ASCII 字节（中日韩等）
// 的关键词在 BuildRelevanceKeywords 中本就逐字符独立成词，跨字母表误判概率低，
// 不设长度门槛，保留现有行为。
bool IsEligibleRelevanceKeyword(const std::string& kw) {
    if (kw.empty()) {
        return false;
    }
    bool all_ascii = true;
    for (unsigned char c : kw) {
        if (c >= 0x80) {
            all_ascii = false;
            break;
        }
    }
    if (all_ascii && kw.size() < 3) {
        return false;
    }
    return true;
}

// ── D4：内置中英意图别名表 ─────────────────────────────────────────────
// 目的：让「中文提问 vs 纯英文技能名/描述」也能产生**有区分度的分数**。在此之前
// 中文逐字/bigram 词元永远命中不了英文单词，全部候选得 0 分后只能退化成
// relevance_filter.zero_hit_keep_all 全量保留——而全量保留在小 context 模型上等于
// 必然溢出（这正是前沿值上不去的一条真实成因）。
// 同一组内还刻意收纳了英文自身的词形变体（如 calibrate/calibration/calibrating）：
// ScoreRelevance() 的描述侧是单向 find，"calibrate" 命不中 "calibration"，靠别名组
// 补齐。纯静态查表，零额外推理延迟。
// 全部小写；匹配与扩展见 ExpandIntentAliases()。
const std::vector<std::vector<std::string>>& IntentAliasGroups() {
    static const std::vector<std::vector<std::string>> kGroups = {
        {"天气", "气象", "weather", "forecast"},
        {"时间", "时刻", "time", "clock", "timezone"},
        {"日期", "日历", "日程", "date", "calendar", "schedule"},
        {"文件", "档案", "file", "files", "filesystem"},
        {"目录", "folder", "directory"},
        {"代码", "code", "coding", "source"},
        {"搜索", "查找", "检索", "search", "find", "query", "lookup"},
        {"翻译", "translate", "translation"},
        {"邮件", "邮箱", "email", "mail", "inbox"},
        {"图片", "图像", "image", "picture", "photo"},
        {"音频", "声音", "audio", "sound", "voice"},
        {"视频", "video", "movie"},
        {"数据库", "database", "sql"},
        {"网络", "网页", "network", "http", "url"},
        {"日志", "log", "logs", "logging"},
        {"测试", "test", "testing", "tests"},
        {"校准", "标定", "calibrate", "calibration", "calibrating", "calibrated"},
        {"通量", "磁通", "flux"},
        {"电容", "电容器", "capacitor", "capacitance"},
        {"配置", "设置", "config", "configuration", "configure", "settings"},
        {"部署", "deploy", "deployment"},
        {"监控", "监测", "monitor", "monitoring"},
        {"备份", "backup", "backups"},
        {"加密", "encrypt", "encryption", "crypto"},
        {"报告", "报表", "report", "reporting"},
        {"地图", "导航", "map", "maps", "navigation"},
        {"股票", "金融", "stock", "stocks", "finance"},
        {"新闻", "news"},
        {"音乐", "music", "song"},
        {"提醒", "闹钟", "reminder", "remind", "alarm"},
        {"诊断", "diagnose", "diagnostics", "diagnosis"},
        {"分析", "analyze", "analysis", "analytics"},
        {"优化", "optimize", "optimization"},
        {"摘要", "总结", "summary", "summarize", "summarization"},
        {"换算", "单位", "convert", "conversion", "unit"},
        {"温度", "temperature", "thermal"},
        {"压力", "pressure"},
        {"传感器", "sensor", "sensors"},
        {"固件", "firmware"},
        {"电池", "电源", "battery", "power"},
        {"用户", "账号", "user", "account"},
        {"权限", "认证", "授权", "permission", "auth", "authorization"},
        {"通知", "notify", "notification"},
        {"任务", "作业", "task", "job"},
        {"工作流", "流水线", "workflow", "pipeline"},
        {"版本", "发布", "version", "release"},
        {"缓存", "cache", "caching"},
        {"队列", "queue"},
        {"索引", "index", "indexing"},
        {"迁移", "migrate", "migration"},
        {"校验", "验证", "validate", "validation", "verify", "checksum"},
    };
    return kGroups;
}

// 判断一个（已小写化的）关键词是否命中某个别名组组员。
// 关键词侧可能是中文 bigram（如"校准"）或英文整词；组员侧可能比关键词更长
// （如组员"传感器" vs bigram 关键词"传感"），因此做受限的双向包含判断：
//   - 完全相等；或
//   - 关键词长度 >= 2 字节且是组员的子串（覆盖 CJK bigram 命中三字词）；或
//   - 组员长度 >= 3 字节且是关键词的子串（覆盖长词包含短组员）。
bool AliasMemberHit(const std::string& lower_kw, const std::string& member) {
    if (lower_kw == member) return true;
    if (lower_kw.size() >= 2 && member.find(lower_kw) != std::string::npos) return true;
    if (member.size() >= 3 && lower_kw.find(member) != std::string::npos) return true;
    return false;
}

// D4：把命中别名组的关键词扩展为同组全部组员（去重后追加到 keywords 末尾）。
// 只做追加、不删除任何原有关键词，因此 intent_aliases_enabled=false 时不调用本函数
// 即逐字节回退到 D4 引入前的关键词集合。
void ExpandIntentAliases(std::vector<std::string>& keywords) {
    std::unordered_set<std::string> present(keywords.begin(), keywords.end());
    std::vector<std::string> additions;
    for (const auto& group : IntentAliasGroups()) {
        bool group_hit = false;
        for (const auto& kw : keywords) {
            for (const auto& member : group) {
                if (AliasMemberHit(kw, member)) { group_hit = true; break; }
            }
            if (group_hit) break;
        }
        if (!group_hit) continue;
        for (const auto& member : group) {
            if (present.insert(member).second) {
                additions.push_back(member);
            }
        }
    }
    keywords.insert(keywords.end(), additions.begin(), additions.end());
}

// D4/D2：从 SKILL description 正文里提取「信号行」——tags / 触发条件这类高价值短行。
// 服务端 <available_skills> XML 只有 <name>/<description>/<location>（见
// ParseAvailableSkillsXml），没有 <tags> 标签，因此 tags/触发条件只能从 description
// 文本里按前缀识别；这不是新协议面，只是对既有文本的结构化利用。
// out_lines 收集**整行原文**（供 L1 档位渲染），返回值是去掉前缀后的取值拼接
// （供 ScoreRelevance 的 tag_weight 打分，不含前缀噪声）。
std::string CollectSkillSignalLines(const std::string& description,
                                    std::vector<std::string>* out_lines) {
    static const std::vector<std::string> kPrefixes = {
        "tags:", "tag:", "keywords:", "triggers:", "trigger:", "use when:", "used when:",
        "标签:", "标签：", "触发:", "触发：", "触发条件:", "触发条件：", "适用于:", "适用于：",
    };
    std::string values;
    std::istringstream iss(description);
    std::string line;
    while (std::getline(iss, line)) {
        // 去掉行首空白与 Markdown 列表/加粗前缀，便于前缀匹配
        size_t begin = line.find_first_not_of(" \t\r-*#");
        if (begin == std::string::npos) continue;
        std::string trimmed = line.substr(begin);
        std::string lower_trimmed = trimmed;
        std::transform(lower_trimmed.begin(), lower_trimmed.end(), lower_trimmed.begin(),
                       [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        for (const auto& prefix : kPrefixes) {
            if (lower_trimmed.compare(0, prefix.size(), prefix) == 0) {
                if (out_lines) {
                    std::string one = trimmed;
                    if (!one.empty() && one.back() == '\r') one.pop_back();
                    out_lines->push_back(one);
                }
                values += trimmed.substr(prefix.size());
                values += " ";
                break;
            }
        }
    }
    return values;
}

} // namespace

std::vector<std::string> PromptOptimizer::BuildRelevanceKeywords(
    const nlohmann::ordered_json& request_data) const
{
    std::vector<std::string> keywords;
    if (!request_data.contains("messages") || !request_data["messages"].is_array()) {
        return keywords;
    }

    const auto& messages = request_data["messages"];
    const size_t recent_window = model_config_.GetPromptOptimizationConfig().recent_window;

    // 复用 message_pre_filter 中 "最近 N 条非 system 消息视为新消息" 的窗口语义，
    // 只取窗口内 role=="user" 的消息文本作为匹配语料
    size_t non_system_count = 0;
    for (const auto& msg : messages) {
        if (!msg.is_object()) continue;
        if (get_json_value<std::string>(msg, "role", "") != "system") {
            ++non_system_count;
        }
    }
    size_t old_cutoff = (non_system_count > recent_window) ? (non_system_count - recent_window) : 0;

    std::string corpus;
    size_t non_system_idx = 0;
    for (const auto& msg : messages) {
        if (!msg.is_object()) continue;
        std::string role = get_json_value<std::string>(msg, "role", "");
        if (role == "system") continue;

        bool is_recent = (non_system_idx >= old_cutoff);
        ++non_system_idx;
        if (!is_recent || role != "user") continue;

        // content 可能是字符串也可能是 OpenAI 多段数组，统一走双格式兼容读取
        std::string text = SecurityUtils::ExtractMessageContentText(msg);
        if (!text.empty()) {
            corpus += text;
            corpus += " ";
        }
    }

    if (corpus.empty()) {
        return keywords;
    }

    std::string lower_corpus = ToLower(corpus);

    // 按非字母数字字符（含中文/全角标点、空格等）为边界切词，中文汉字（无空格分隔）
    // 逐字符视为独立词元；返回去重后的关键词列表
    std::unordered_set<std::string> seen;
    auto flush_token = [&](std::string& token) {
        if (!token.empty()) {
            if (seen.insert(token).second) {
                keywords.push_back(token);
            }
            token.clear();
        }
    };

    // CJK 相邻二字组（bigram）：BuildRelevanceKeywords 默认逐字符切分中文（单字词元
    // 噪声大，如"的"几乎命中一切），额外生成相邻两个非标点 CJK 字符的二字组
    // （如"上海的天气怎么样"生成"上海""海的""的天""天气"...），提升"中文提问 vs
    // 中文技能描述"场景的匹配精度；不跨标点/空格生成，也不产生中英翻译映射（不会让
    // "天气"命中"weather"——中文提问命中纯英文工具名真正生效的是下面 Filter 函数里
    // 的 zero_hit_keep_all 全零分兜底，这里只是锦上添花）。cjk_bigram=false 时逐字节
    // 回退：不生成任何 bigram 词元，只保留单字词元。
    const bool cjk_bigram_enabled = model_config_.GetPromptOptimizationConfig().relevance_filter.cjk_bigram;

    std::string current_token;
    std::string prev_cjk_char;  // 上一个连续 CJK 字符，遇到 ASCII/标点边界时清空
    size_t i = 0;
    while (i < lower_corpus.size()) {
        auto [cp, len] = DecodeUtf8CodePoint(lower_corpus, i);
        if (cp < 0x80) {
            if (std::isalnum(static_cast<int>(cp))) {
                current_token += static_cast<char>(cp);
            } else {
                flush_token(current_token);
            }
            prev_cjk_char.clear();  // ASCII 边界，中断 bigram 连续性
        } else if (IsCjkPunctuationOrSpace(cp)) {
            flush_token(current_token);
            prev_cjk_char.clear();  // 标点边界，bigram 不跨标点生成
        } else {
            // 非标点的多字节字符（如中文汉字）：与 ASCII 词元切分开，逐字符独立成词
            flush_token(current_token);
            std::string ch = lower_corpus.substr(i, len);
            if (seen.insert(ch).second) {
                keywords.push_back(ch);
            }
            if (cjk_bigram_enabled && !prev_cjk_char.empty()) {
                std::string bigram = prev_cjk_char + ch;
                if (seen.insert(bigram).second) {
                    keywords.push_back(bigram);
                }
            }
            prev_cjk_char = ch;
        }
        i += len;
    }
    flush_token(current_token);

    // D4：跨语言意图别名扩展（纯静态查表，零额外推理延迟）。只追加同组别名，
    // 不删除任何原有关键词；intent_aliases_enabled=false 时整段跳过，
    // 关键词集合与 D4 引入前逐字节一致。
    if (model_config_.GetPromptOptimizationConfig().relevance_filter.intent_aliases_enabled) {
        size_t before = keywords.size();
        ExpandIntentAliases(keywords);
        My_Log{My_Log::Level::kInfo} << "[BuildRelevanceKeywords] intent alias expansion: "
                                      << before << " -> " << keywords.size() << " keyword(s)" << std::endl;
    }

    My_Log{My_Log::Level::kInfo} << "[BuildRelevanceKeywords] Extracted " << keywords.size()
                                  << " keyword(s) from recent_window=" << recent_window
                                  << " user message(s)" << std::endl;
    return keywords;
}

size_t PromptOptimizer::ScoreRelevance(
    const std::string& name,
    const std::string& description,
    const std::vector<std::string>& keywords,
    const std::string& tags) const
{
    if (keywords.empty()) {
        return 0;
    }

    const auto& relevance_cfg = model_config_.GetPromptOptimizationConfig().relevance_filter;
    size_t score = 0;

    // 名称按 '-'/'_' 拆分为子词，每个子词若与任一 keyword 发生子串双向匹配
    // （大小写不敏感）即命中，命中一次加 name_token_weight
    std::string lower_name = ToLower(name);
    std::vector<std::string> name_subwords;
    {
        std::string token;
        for (char c : lower_name) {
            if (c == '-' || c == '_') {
                if (!token.empty()) { name_subwords.push_back(token); token.clear(); }
            } else {
                token += c;
            }
        }
        if (!token.empty()) name_subwords.push_back(token);
    }

    for (const auto& subword : name_subwords) {
        if (subword.empty()) continue;
        for (const auto& kw : keywords) {
            if (!IsEligibleRelevanceKeyword(kw)) continue;
            if (subword.find(kw) != std::string::npos || kw.find(subword) != std::string::npos) {
                score += relevance_cfg.name_token_weight;
                break; // 该子词只计一次分，避免同一子词命中多个 keyword 时被重复加分
            }
        }
    }

    // 描述做关键词命中计数（子串匹配，大小写不敏感），每命中一个 keyword 加
    // description_keyword_weight
    if (!description.empty()) {
        std::string lower_desc = ToLower(description);
        for (const auto& kw : keywords) {
            if (!IsEligibleRelevanceKeyword(kw)) continue;
            if (lower_desc.find(kw) != std::string::npos) {
                score += relevance_cfg.description_keyword_weight;
            }
        }
    }

    // D4：tags 段（从 description 正文提取的 "tags:"/"触发条件:" 等信号行取值）额外
    // 做一轮关键词命中计数，每命中一个加 tag_weight。目的是让**排序有区分度**：
    // 长 description 里一个偶然出现的词与 tags 里显式声明的意图词不应等价。
    // tag_weight=0 或 tags 为空时整段跳过，逐字节等价于 D4 引入前。
    if (!tags.empty() && relevance_cfg.tag_weight > 0) {
        std::string lower_tags = ToLower(tags);
        for (const auto& kw : keywords) {
            if (!IsEligibleRelevanceKeyword(kw)) continue;
            if (lower_tags.find(kw) != std::string::npos) {
                score += relevance_cfg.tag_weight;
            }
        }
    }

    return score;
}

RuntimeSkillMappings PromptOptimizer::FilterSkillsByRelevance(
    const RuntimeSkillMappings& all_skills,
    const std::vector<std::string>& keywords,
    size_t token_budget) const
{
    if (all_skills.empty()) {
        return RuntimeSkillMappings{};
    }
    if (keywords.empty()) {
        // 无关键词可用（通常是调用方未提供 request_data，或本轮
        // recent_window 窗口内没有任何用户文本），这是"无法判断相关性"而
        // 非"确定无关"，直接跳过过滤、原样返回全量，避免误伤
        My_Log{My_Log::Level::kInfo} << "[FilterSkillsByRelevance] Empty keywords, bypassing filter ("
                                      << all_skills.size() << " skill(s) kept as-is)" << std::endl;
        return all_skills;
    }

    RuntimeSkillMappings filtered;

    // 打分并过滤掉零分项
    std::vector<std::pair<std::string, size_t>> scored; // name -> score
    scored.reserve(all_skills.size());
    for (const auto& [name, info] : all_skills) {
        // D4：tags 从 description 正文里提取（服务端 XML 无 <tags> 标签），参与打分；
        // tag_weight=0 时 ScoreRelevance 内部会整段跳过，逐字节等价于 D4 引入前。
        std::string tags = CollectSkillSignalLines(info.use_for, nullptr);
        size_t score = ScoreRelevance(name, info.use_for, keywords, tags);
        if (score > 0) {
            scored.emplace_back(name, score);
        }
    }

    // 全零分兜底：keywords 非空但全部候选都得 0 分（典型场景：中文提问 vs 纯英文
    // SKILL 名，中文逐字/bigram 分词永远不会匹配英文单词），与上面 keywords.empty()
    // 分支是同一性质的"无法判断相关性≠确定无关"，理应同样整段回退保留，而不是
    // 让技能目录对模型隐身。zero_hit_keep_all=false 时逐字节回退当前行为（全零分
    // 仍返回空集合）。
    const auto& relevance_cfg = model_config_.GetPromptOptimizationConfig().relevance_filter;
    if (scored.empty()) {
        if (relevance_cfg.zero_hit_keep_all) {
            My_Log{My_Log::Level::kInfo} << "[FilterSkillsByRelevance] All " << all_skills.size()
                                          << " candidate(s) scored zero, zero_hit_keep_all bypassing filter ("
                                          << all_skills.size() << " skill(s) kept as-is)" << std::endl;
            return all_skills;
        }
        My_Log{My_Log::Level::kInfo} << "[FilterSkillsByRelevance] All " << all_skills.size()
                                      << " candidate(s) scored zero, zero_hit_keep_all disabled, "
                                         "returning empty set (legacy behavior)" << std::endl;
        return filtered;
    }

    // 按分数降序排序；分数相同时用名称做次级排序键，避免 unordered_map 遍历顺序不确定
    std::sort(scored.begin(), scored.end(), [](const auto& a, const auto& b) {
        if (a.second != b.second) return a.second > b.second;
        return a.first < b.first;
    });

    // 按 token 预算贪心保留，超预算即停止
    size_t used_tokens = 0;
    for (const auto& entry : scored) {
        const std::string& name = entry.first;
        const auto& info = all_skills.at(name);
        // 用 name+path+use_for 拼接近似估算这条 SKILL 加入 Catalog 后的 token 占用
        std::string approx = name + " " + info.path + " " + info.use_for;
        size_t item_tokens = CountTokens(approx);
        if (used_tokens + item_tokens > token_budget) {
            break;
        }
        used_tokens += item_tokens;
        filtered[name] = info;
    }

    My_Log{My_Log::Level::kInfo} << "[FilterSkillsByRelevance] " << all_skills.size()
                                  << " candidate(s) -> " << filtered.size()
                                  << " kept within token_budget=" << token_budget << std::endl;
    return filtered;
}

// ============================================================
// D2：技能目录三档渐进披露
// ============================================================

std::vector<ScoredSkill> PromptOptimizer::AssignSkillDetailLevels(
    const RuntimeSkillMappings& all_skills,
    const std::vector<std::string>& keywords,
    size_t skills_token_budget) const
{
    std::vector<ScoredSkill> result;
    if (all_skills.empty()) {
        return result;
    }

    const auto& po_cfg = model_config_.GetPromptOptimizationConfig();
    const auto& relevance_cfg = po_cfg.relevance_filter;
    const auto& disclosure_cfg = po_cfg.skill_disclosure;

    // 1) 打分。零分丢弃 / keywords 为空 / 全零分兜底 三条语义与
    //    FilterSkillsByRelevance 完全一致（不另起一套判据），区别只在“保留下来的
    //    那些怎么展示”。
    std::vector<ScoredSkill> candidates;
    candidates.reserve(all_skills.size());
    bool any_positive = false;
    for (const auto& [name, info] : all_skills) {
        ScoredSkill s;
        s.name = name;
        s.description = info.use_for;
        s.location = info.path;
        s.tags = CollectSkillSignalLines(info.use_for, nullptr);
        s.score = keywords.empty() ? 0 : ScoreRelevance(name, info.use_for, keywords, s.tags);
        if (s.score > 0) any_positive = true;
        candidates.push_back(std::move(s));
    }

    if (!keywords.empty() && !any_positive && !relevance_cfg.zero_hit_keep_all) {
        My_Log{My_Log::Level::kInfo} << "[AssignSkillDetailLevels] All " << all_skills.size()
                                      << " candidate(s) scored zero, zero_hit_keep_all disabled, "
                                         "returning empty set (legacy behavior)" << std::endl;
        return result;
    }
    if (!keywords.empty() && any_positive) {
        // 零分丢弃（与 FilterSkillsByRelevance 一致：至少有一个正分候选时才丢弃零分项）
        candidates.erase(std::remove_if(candidates.begin(), candidates.end(),
                                        [](const ScoredSkill& s) { return s.score == 0; }),
                         candidates.end());
    }

    // 2) 按分数降序（同分按名称）排序，避开 unordered_map 遍历顺序不确定
    std::sort(candidates.begin(), candidates.end(), [](const ScoredSkill& a, const ScoredSkill& b) {
        if (a.score != b.score) return a.score > b.score;
        return a.name < b.name;
    });

    // 3) 按“Top-K 期望档位 + 预算降档”分配。关键：预算不够时**降档**
    //    （L2→L1→L0），只有连 L0 单行都放不进时才真正丢弃。
    size_t used_tokens = 0;
    size_t dropped = 0;
    for (size_t idx = 0; idx < candidates.size(); ++idx) {
        ScoredSkill entry = candidates[idx];
        SkillDetailLevel desired = SkillDetailLevel::kNameOnly;
        if (idx < disclosure_cfg.l2_top_k) {
            desired = SkillDetailLevel::kFull;
        } else if (idx < disclosure_cfg.l2_top_k + disclosure_cfg.l1_top_k) {
            desired = SkillDetailLevel::kSummary;
        }

        bool placed = false;
        for (;;) {
            entry.level = desired;
            size_t item_tokens = CountTokens(RenderSkillEntry(entry));
            if (used_tokens + item_tokens <= skills_token_budget) {
                used_tokens += item_tokens;
                result.push_back(entry);
                placed = true;
                break;
            }
            if (desired == SkillDetailLevel::kFull) {
                desired = SkillDetailLevel::kSummary;
            } else if (desired == SkillDetailLevel::kSummary) {
                desired = SkillDetailLevel::kNameOnly;
            } else {
                break; // 连 L0 都放不进，真正丢弃
            }
        }
        if (!placed) {
            // 与旧行为一致：保留集始终是按分数排序后的一个前缀，超预算即停
            dropped = candidates.size() - idx;
            break;
        }
    }

    size_t l2 = 0, l1 = 0, l0 = 0;
    for (const auto& s : result) {
        if (s.level == SkillDetailLevel::kFull) ++l2;
        else if (s.level == SkillDetailLevel::kSummary) ++l1;
        else ++l0;
    }
    My_Log{My_Log::Level::kInfo} << "[AssignSkillDetailLevels] " << all_skills.size()
                                  << " candidate(s) -> " << result.size()
                                  << " kept (L2=" << l2 << ", L1=" << l1 << ", L0=" << l0
                                  << ", dropped=" << dropped
                                  << ") within skills_token_budget=" << skills_token_budget
                                  << " (used=" << used_tokens << ")" << std::endl;
    return result;
}

std::string PromptOptimizer::RenderSkillEntry(const ScoredSkill& skill) const
{
    const auto& disclosure_cfg = model_config_.GetPromptOptimizationConfig().skill_disclosure;
    std::ostringstream oss;

    switch (skill.level) {
        case SkillDetailLevel::kFull:
            // L2：与 D2 引入前 BuildStructuredSkillCatalog 的条目渲染**逐字节一致**
            //（Path 行 + use_for 非空时的 Use for 行 + 一个空行，不输出技能名标题）。
            oss << "Path: " << skill.location << "\n";
            if (!skill.description.empty()) {
                oss << "Use for: " << skill.description << "\n";
            }
            oss << "\n";
            break;
        case SkillDetailLevel::kSummary: {
            // L1：结构与 L2 一致，只把 description 换成摘要，并把从 description 里提取的
            // 触发条件/tags 行单独补回——保住“什么时候用”这一最高信号的部分，
            // 舍弃步骤/示例类长正文（那些内容模型 read(SKILL.md) 后一样能拿到）。
            std::vector<std::string> signal_lines;
            CollectSkillSignalLines(skill.description, &signal_lines);
            oss << "Path: " << skill.location << "\n";
            if (!skill.description.empty()) {
                oss << "Use for: "
                    << safe_utf8_truncate(skill.description, disclosure_cfg.l1_summary_max_chars, "...")
                    << "\n";
            }
            for (const auto& line : signal_lines) {
                oss << line << "\n";
            }
            oss << "\n";
            break;
        }
        case SkillDetailLevel::kNameOnly:
        default:
            // L0：单行。保留 path 是必需的——skill_rule 要求模型先 read(SKILL.md)，
            // 没有 path 就算选对了也无法拉取正文。
            oss << "- " << skill.name << " -> " << skill.location;
            if (!skill.description.empty()) {
                oss << " (" << safe_utf8_truncate(skill.description, disclosure_cfg.l0_summary_max_chars, "...") << ")";
            }
            oss << "\n";
            break;
    }
    return oss.str();
}

std::string PromptOptimizer::BuildLeveledSkillCatalog(const std::vector<ScoredSkill>& skills) const
{
    if (skills.empty()) {
        return "";
    }
    std::ostringstream oss;
    // 头部说明与 BuildStructuredSkillCatalog 完全一致（同一个配置项与开关），
    // 只有条目体按档位变化；全部为 L2 时与旧输出逐字节相同（除排序变为按分数）。
    const auto& config = model_config_.GetPromptOptimizationConfig();
    if (config.system_prompts.sections_enabled.catalog_structured_intro &&
        !config.system_prompts.catalog_structured_intro.empty()) {
        oss << config.system_prompts.catalog_structured_intro;
    }
    for (const auto& skill : skills) {
        oss << RenderSkillEntry(skill);
    }
    return oss.str();
}

nlohmann::ordered_json PromptOptimizer::FilterToolsByRelevance(
    const nlohmann::ordered_json& tools_array,
    const std::vector<std::string>& keywords,
    size_t token_budget) const
{
    if (!tools_array.is_array() || tools_array.empty()) {
        return nlohmann::ordered_json::array();
    }
    if (keywords.empty()) {
        // 无关键词可用，同 FilterSkillsByRelevance：视为"无法判断相关性"，
        // 直接跳过过滤、原样返回 tools_array 全量
        My_Log{My_Log::Level::kInfo} << "[FilterToolsByRelevance] Empty keywords, bypassing filter ("
                                      << tools_array.size() << " tool(s) kept as-is)" << std::endl;
        return tools_array;
    }

    nlohmann::ordered_json filtered = nlohmann::ordered_json::array();

    // 打分并过滤掉零分项
    std::vector<std::pair<size_t, size_t>> scored; // (tools_array 中的下标, score)
    scored.reserve(tools_array.size());
    for (size_t idx = 0; idx < tools_array.size(); ++idx) {
        const auto& tool = tools_array[idx];
        if (!tool.contains("function") || !tool["function"].contains("name")) continue;
        // name 字段必须显式校验 is_string()：畸形/攻击性 tools 数组（如 name 为数字）
        // 不应导致未捕获的 json::type_error，而是跳过该工具（视为不相关，不崩溃）
        if (!tool["function"]["name"].is_string()) continue;
        std::string name = tool["function"]["name"].get<std::string>();
        // description 同理：字段存在但非字符串（畸形/攻击性输入）时不调用 .value()
        // 的隐式 get<T>()，直接按空描述处理，避免同类 json::type_error
        std::string description;
        if (tool["function"].contains("description") && tool["function"]["description"].is_string()) {
            description = tool["function"]["description"].get<std::string>();
        }
        size_t score = ScoreRelevance(name, description, keywords);
        if (score > 0) {
            scored.emplace_back(idx, score);
        }
    }

    // 全零分兜底：与 FilterSkillsByRelevance 同一性质的"无法判断相关性≠确定无关"，
    // 但工具的完整 JSON Schema 体量远大于 SKILL 目录条目，无条件全量保留可能反而
    // 直接把预算打爆，因此不做"全量保留"，而是按 token 预算、原始顺序（无分数可排序，
    // 保留客户端原始意图顺序）贪心 Top-K——复用与下方完全相同的预算累加方式，不发明
    // 新的截断方式。zero_hit_keep_all=false 时逐字节回退当前行为（全零分仍返回空数组）。
    const auto& relevance_cfg = model_config_.GetPromptOptimizationConfig().relevance_filter;
    if (scored.empty()) {
        if (!relevance_cfg.zero_hit_keep_all) {
            My_Log{My_Log::Level::kInfo} << "[FilterToolsByRelevance] All " << tools_array.size()
                                          << " candidate(s) scored zero, zero_hit_keep_all disabled, "
                                             "returning empty set (legacy behavior)" << std::endl;
            return filtered;
        }
        size_t zero_hit_used_tokens = 0;
        for (size_t idx = 0; idx < tools_array.size(); ++idx) {
            const auto& tool = tools_array[idx];
            size_t item_tokens = CountTokens(tool.dump());
            if (zero_hit_used_tokens + item_tokens > token_budget) {
                break;
            }
            zero_hit_used_tokens += item_tokens;
            filtered.push_back(tool);
        }
        My_Log{My_Log::Level::kInfo} << "[FilterToolsByRelevance] All " << tools_array.size()
                                      << " candidate(s) scored zero, zero_hit_keep_all fallback -> "
                                      << filtered.size() << " kept (original order) within token_budget="
                                      << token_budget << std::endl;
        return filtered;
    }

    // 按分数降序排序（分数相同保持原始顺序，用 stable_sort）
    std::stable_sort(scored.begin(), scored.end(), [](const auto& a, const auto& b) {
        return a.second > b.second;
    });

    // 按 token 预算贪心保留 Top-K
    size_t used_tokens = 0;
    for (const auto& entry : scored) {
        size_t idx = entry.first;
        const auto& tool = tools_array[idx];
        size_t item_tokens = CountTokens(tool.dump());
        if (used_tokens + item_tokens > token_budget) {
            break;
        }
        used_tokens += item_tokens;
        filtered.push_back(tool);
    }

    My_Log{My_Log::Level::kInfo} << "[FilterToolsByRelevance] " << tools_array.size()
                                  << " candidate(s) -> " << filtered.size()
                                  << " kept within token_budget=" << token_budget << std::endl;
    return filtered;
}

bool PromptOptimizer::HasBudgetContention(const nlohmann::ordered_json& request_data) const
{
    // tools 侧候选：请求带了非空 tools 数组
    bool has_tools = request_data.contains("tools") && request_data["tools"].is_array() &&
                     !request_data["tools"].empty();
    if (!has_tools) {
        return false;
    }
    // skills 侧候选：第一个 system 消息里带 <available_skills> 目录块。
    // 这里只做子串探测（不重新解析整份 XML），与 ExtractSkillsFromRequest 同一判据，
    // 避免为预算判定引入第二次完整解析开销。
    if (!request_data.contains("messages") || !request_data["messages"].is_array()) {
        return false;
    }
    for (const auto& msg : request_data["messages"]) {
        if (!msg.contains("role") || !msg["role"].is_string()) continue;
        if (msg["role"].get<std::string>() != "system") continue;
        if (!msg.contains("content") || !msg["content"].is_string()) break;
        const std::string& content = msg["content"].get_ref<const std::string&>();
        return content.find("<available_skills>") != std::string::npos;
    }
    return false;
}

size_t PromptOptimizer::ComputeRelevanceTokenBudget(BudgetPartitionKind kind,
                                                    const nlohmann::ordered_json* request_data) const
{
    // 复用与 model_input_builder.h 中 tools_budget 完全相同的"可用上下文空间"
    // 推导：context_size 减去按 output_reserve_ratio 预留的输出空间。这不是为
    // 相关性筛选新引入的硬编码魔数，而是复用已有的、语义上"这部分内容最多能占多少"
    // 的现有配置概念。BuildSystemContext/BuildDynamicToolsIntro/
    // FilterToolsIntroByRequest/OptimizeHarmonyDeveloperMessage/
    // ConvertToolsToOptimizedTypeScript 均未接收显式的 token_budget 参数，
    // 需要自行推导时统一调用此方法。
    const auto& config = model_config_.GetPromptOptimizationConfig();
    int total_context = model_config_.context_size();
    int reserved_output = static_cast<int>(total_context * config.output_reserve_ratio);
    size_t total_budget = static_cast<size_t>(std::max(total_context - reserved_output, 0));

    // D3：预算分区（skills / tools 两个分区，history 仍由 message_pre_filter 自己的
    // token 预算控制，不在此处引入重复口径）。enabled=false 时逐字节回退为单一
    // 总预算语义（两种 kind 都返回 total_budget，与 D3 引入前完全一致）。
    const auto& bp_cfg = config.budget_partition;
    if (!bp_cfg.enabled) {
        return total_budget;
    }

    // 关键语义：分区只在真的发生竞争时生效。没有竞争（只有 tools 没有 skills，
    // 或反之）时两种 kind 都拿到完整总预算——D3 的目的是"不让一方把另一方挤没"，
    // 不是"无条件按比例削减双方"。拿不到请求上下文时同样按无竞争处理（保守，
    // 不削减既有行为）。
    if (request_data == nullptr || !HasBudgetContention(*request_data)) {
        return total_budget;
    }

    // 竞争成立：按**实际占用**而不是"对象是否出现"来约束（这一条是外部独立评审
    // 与代码复核共同得出的结论，务必保持）。早先的实现一旦 skills/tools 同时在场
    // 就固定按 65/35 切，等于把"skills 保底 15%"实现成了"skills 限额 65%"——语义
    // 完全翻转，且 tools 只用 200 token 时也会白占 35% 预算、把技能目录无谓截断。
    //
    // 现在的语义：
    //   - 先按 tools 的**真实体量**估算它需要多少 token（纯字符串计算，零额外推理）；
    //   - tools 需求未超过 tools_ratio 上限时，它只拿走自己真正需要的那部分，
    //     skills 拿走全部剩余（不被无故削减）；
    //   - tools 需求超过上限时才截到 tools_ratio，此时 skills 拿剩余，并由
    //     skills_floor_ratio 兜住下限（真正的"保底"语义）。
    size_t skills_floor = static_cast<size_t>(static_cast<double>(total_budget) * bp_cfg.skills_floor_ratio);
    size_t tools_budget_cap = static_cast<size_t>(static_cast<double>(total_budget) * bp_cfg.tools_ratio);

    // tools 真实需求估算：直接量 tools 数组序列化后的字节数，按 CJK 感知
    // bytes/token 比例折成 token（与 P1 同一口径，不引入第二套换算）。
    size_t tools_need_tokens = 0;
    if (request_data->contains("tools") && (*request_data)["tools"].is_array()) {
        std::string tools_text = (*request_data)["tools"].dump();
        const auto& fid_cfg = config.fidelity;
        double bytes_per_token = EstimateCjkAwareBytesPerToken(
            tools_text, fid_cfg.cjk_bytes_per_token, fid_cfg.ascii_bytes_per_token);
        if (bytes_per_token > 0.0) {
            tools_need_tokens = static_cast<size_t>(static_cast<double>(tools_text.length()) / bytes_per_token);
        }
    }

    size_t tools_budget = std::min(tools_need_tokens, tools_budget_cap);
    size_t skills_budget = (total_budget > tools_budget) ? (total_budget - tools_budget) : 0;
    if (skills_budget < skills_floor) {
        skills_budget = std::min(skills_floor, total_budget);
        tools_budget = (total_budget > skills_budget) ? (total_budget - skills_budget) : 0;
    }

    switch (kind) {
        case BudgetPartitionKind::kSkills:
            return skills_budget;
        case BudgetPartitionKind::kTools:
        default:
            // 直接返回 933 行已算好的 tools_budget（= min(真实需求, 上限)）。
            // 注意：不能再用 std::max(tools_budget, min(need, cap)) 撤销 skills_floor
            // 咬合后的削减——那会让 skills+tools > total_budget，使
            // skills_floor_ratio 变成空操作（已被外部评审 + 代码复核共同证伪）。
            return tools_budget;
    }
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
    const std::string& tool_prompt_template,
    size_t token_budget,
    const nlohmann::ordered_json& request_data)
{
    My_Log{My_Log::Level::kDebug} << "[Optimizer] Processing tool descriptions" << std::endl;

    std::string result;

    // 用给定模板包装工具签名文本，并去掉末尾多余换行（避免 </tools> 前出现空行）
    auto wrap = [&](std::string descs) {
        while (!descs.empty() && descs.back() == '\n') descs.pop_back();
        return tool_prompt_template.empty() ? descs : str_replace(tool_prompt_template, "{tool_descs}", descs);
    };

    // tool_descriptions 现在直接是原始 JSON 数组字符串
    try {
        json tools_array = json::parse(tool_descriptions);

        // Tier 0（相关性筛选，在 Tier1 逐级压缩之前）：按本轮问题缩小候选工具集合，
        // 未命中的工具整条不出现，而不是被压缩得更简。request_data 为空 object 时
        // （调用点拿不到真实请求上下文）BuildRelevanceKeywords 返回空 keywords，
        // FilterToolsByRelevance 会直接跳过过滤、保留全部候选，不会误伤。
        if (tools_array.is_array()) {
            const auto& relevance_cfg = model_config_.GetPromptOptimizationConfig().relevance_filter;
            if (relevance_cfg.enabled) {
                std::vector<std::string> keywords = BuildRelevanceKeywords(request_data);
                tools_array = FilterToolsByRelevance(tools_array, keywords,
                                                     ComputeRelevanceTokenBudget(BudgetPartitionKind::kTools, &request_data));
            }
        }

        if (tools_array.is_array()) {
            size_t original_tokens = CountTokens(tool_descriptions);

            // PromptLedger 可观测：记录本次实际落到的降级档位（1~4）与相关性过滤后
            // 候选总数/最终保留数——不新增第二套统计，直接写入既有 last_stats_。
            int tier = 1;
            size_t tools_total = tools_array.size();
            size_t tools_kept_count = 0;

            // Tier 1：已知工具用 GetOptimizedToolDefinition 的预定义精简定义；
            // 未知工具退化为 GenerateBasicTypeScriptDefinition
            // 生成的基础签名，而不是原样保留完整 JSON Schema——这曾是压缩对映射表外
            // 工具完全失效、只能把它们原样带过的直接原因。
            std::string optimized_tools;
            for (const auto& tool : tools_array) {
                std::string tool_name;
                if (tool.contains("function") && tool["function"].contains("name")) {
                    tool_name = tool["function"]["name"].get<std::string>();
                }
                if (tool_name == "image") {
                    My_Log{My_Log::Level::kDebug} << "[Optimizer] Filtered out 'image' tool" << std::endl;
                    continue;
                }

                std::string optimized_def = GetOptimizedToolDefinition(tool_name);
                if (!optimized_def.empty()) {
                    optimized_tools += optimized_def + "\n";
                    tools_kept_count++;
                    My_Log{My_Log::Level::kDebug} << "[Optimizer] Optimized tool: " << tool_name << std::endl;
                    continue;
                }

                std::string basic_def = GenerateBasicTypeScriptDefinition(tool);
                optimized_tools += (!basic_def.empty() ? basic_def : tool.dump()) + "\n";
                tools_kept_count++;
                My_Log{My_Log::Level::kDebug} << "[Optimizer] Generated basic signature for: " << tool_name << std::endl;
            }

            result = wrap(optimized_tools);

            // Tier 2：仍超预算时剥离 `//` 描述性注释，只保留类型签名骨架
            if (CountTokens(result) > token_budget) {
                optimized_tools = StripToolCommentLines(optimized_tools);
                result = wrap(optimized_tools);
                tier = 2;
                // tools_kept_count 不变：仍是同一批工具，只是剥掉了注释文本。
                My_Log{My_Log::Level::kInfo} << "[Optimizer] Tools still over budget after Tier1, stripped comments" << std::endl;
            }

            // Tier 3：仍超预算时退化为最简单行签名 name(param1, param2?)
            if (CountTokens(result) > token_budget) {
                std::string minimal;
                size_t minimal_kept = 0;
                for (const auto& tool : tools_array) {
                    std::string sig = BuildMinimalToolSignature(tool);
                    if (!sig.empty()) {
                        minimal += sig + "\n";
                        minimal_kept++;
                    }
                }
                optimized_tools = minimal;
                result = wrap(optimized_tools);
                tier = 3;
                tools_kept_count = minimal_kept;
                My_Log{My_Log::Level::kInfo} << "[Optimizer] Tools still over budget after Tier2, degraded to minimal signatures" << std::endl;
            }

            // Tier 4（硬性兜底）：工具数量过多导致极简签名仍超预算时，逐条追加
            // 直至预算用尽即止，保证返回结果的 token 数始终不超过 token_budget。
            if (CountTokens(result) > token_budget) {
                std::istringstream iss(optimized_tools);
                std::string line, truncated;
                size_t truncated_kept = 0;
                while (std::getline(iss, line)) {
                    std::string candidate = truncated + line + "\n";
                    if (CountTokens(wrap(candidate)) > token_budget) break;
                    truncated = candidate;
                    truncated_kept++;
                }
                optimized_tools = truncated;
                result = wrap(optimized_tools);
                tier = 4;
                tools_kept_count = truncated_kept;
                My_Log{My_Log::Level::kWarning} << "[Optimizer] Tool list truncated to fit token budget of "
                                                 << token_budget << " tokens" << std::endl;
            }

            size_t optimized_tokens = CountTokens(optimized_tools);
            float savings = ComputeSavingsPercent(original_tokens, optimized_tokens);

            last_stats_.tools_tier = tier;
            last_stats_.tools_total = tools_total;
            last_stats_.tools_kept = tools_kept_count;

            My_Log{My_Log::Level::kDebug} << "[Optimizer] Tools - Original: " << original_tokens
                                           << " tokens, Optimized: " << optimized_tokens
                                           << " tokens, Savings: " << savings << "%" << std::endl;
        }
    } catch (const std::exception& e) {
        My_Log{} << "Failed to parse/optimize tools: " << e.what() << std::endl;
        // 解析失败：使用原始工具描述，账本档位归零（PromptLedger 约定：tier=0 表示未压缩）。
        result = wrap(tool_descriptions);
        last_stats_.tools_tier = 0;
        last_stats_.tools_total = 0;
        last_stats_.tools_kept = 0;
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

    // 相关性筛选：先缩小候选工具集合，再生成 tools_intro；未命中的工具行直接不出现
    json tools_array = request_data["tools"];
    const auto& relevance_cfg = model_config_.GetPromptOptimizationConfig().relevance_filter;
    if (relevance_cfg.enabled) {
        std::vector<std::string> keywords = BuildRelevanceKeywords(request_data);
        tools_array = FilterToolsByRelevance(tools_array, keywords,
                                             ComputeRelevanceTokenBudget(BudgetPartitionKind::kTools, &request_data));
    }
    if (tools_array.empty()) {
        return "";  // 全部未命中 → 与"无 tools"等价，回退到配置文件硬编码值
    }

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

    // 相关性筛选：先缩小候选工具集合，client_tools 只收集筛选后仍相关的工具名，
    // 未命中的工具行与"客户端未传入此工具"走同一条逐行过滤逻辑，不重复实现
    json relevant_tools = request_data["tools"];
    const auto& relevance_cfg = model_config_.GetPromptOptimizationConfig().relevance_filter;
    if (relevance_cfg.enabled) {
        std::vector<std::string> keywords = BuildRelevanceKeywords(request_data);
        relevant_tools = FilterToolsByRelevance(relevant_tools, keywords,
                                                ComputeRelevanceTokenBudget(BudgetPartitionKind::kTools, &request_data));
    }

    // 收集客户端实际传入且通过相关性筛选的工具名集合
    std::unordered_set<std::string> client_tools;
    for (const auto& tool : relevant_tools) {
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
    // 相关性筛选：在生成 TypeScript 定义之前先按本轮问题缩小候选工具集合，复用
    // 与 OptimizeToolsPrompt 相同的 FilterToolsByRelevance，两条路径不重复实现；
    // 用筛选后的 relevant_tools 判断"是否有工具"，全部未命中时与"客户端未传 tools"
    // 等价，不输出 Tool Usage Guidelines/namespace functions 空壳段落。
    json relevant_tools = tools;
    const auto& relevance_cfg = model_config_.GetPromptOptimizationConfig().relevance_filter;
    if (relevance_cfg.enabled && !tools.is_null() && tools.is_array() && !tools.empty()) {
        std::vector<std::string> keywords = BuildRelevanceKeywords(request_data);
        relevant_tools = FilterToolsByRelevance(tools, keywords,
                                                ComputeRelevanceTokenBudget(BudgetPartitionKind::kTools, &request_data));
    }

    if (!relevant_tools.is_null() && !relevant_tools.empty()) {
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
        
        // relevant_tools 已完成相关性筛选，此处不再传 request_data，让
        // ConvertToolsToOptimizedTypeScript 内部的过滤天然退化为直通（keywords 为空
        // 时的 bypass 语义），避免用同一套 keywords/budget 重复过滤一次
        std::string ts_tools = ConvertToolsToOptimizedTypeScript(relevant_tools);
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

std::string PromptOptimizer::ConvertToolsToOptimizedTypeScript(
    const json& tools,
    const nlohmann::ordered_json& request_data)
{
    // 将 OpenAI JSON 格式的工具定义转换为精简的 TypeScript 格式
    // 根据 openai-harmony.md 第 319-371 行的规范
    
    std::string result;
    size_t tool_count = 0;
    
    if (!tools.is_array()) {
        My_Log{My_Log::Level::kInfo} << "Tools is not an array, skipping conversion" << std::endl;
        return result;
    }

    // 相关性筛选：request_data 默认空 object，调用方若已自行筛选过 tools
    // （如 OptimizeHarmonyDeveloperMessage 内部），不传 request_data 时
    // BuildRelevanceKeywords 返回空 keywords，FilterToolsByRelevance 会直接跳过
    // 过滤、原样返回 tools，天然避免重复过滤；直接外部调用点（如 SUBAGENT Harmony
    // 分支）传入真实 request_data 时才会真正生效。
    json relevant_tools = tools;
    const auto& relevance_cfg = model_config_.GetPromptOptimizationConfig().relevance_filter;
    if (relevance_cfg.enabled) {
        std::vector<std::string> keywords = BuildRelevanceKeywords(request_data);
        relevant_tools = FilterToolsByRelevance(tools, keywords,
                                                ComputeRelevanceTokenBudget(BudgetPartitionKind::kTools, &request_data));
    }
    
    for (const auto& tool : relevant_tools) {
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

std::string PromptOptimizer::StripToolCommentLines(const std::string& text)
{
    // 逐行剥离 `//` 之后的内容（整行注释与行内注释统一处理），
    // 只保留类型签名骨架；纯注释行会被整行丢弃。
    std::istringstream iss(text);
    std::string line, out;
    while (std::getline(iss, line)) {
        size_t comment_pos = line.find("//");
        std::string code_part = (comment_pos != std::string::npos) ? line.substr(0, comment_pos) : line;
        while (!code_part.empty() && (code_part.back() == ' ' || code_part.back() == '\t')) {
            code_part.pop_back();
        }
        if (code_part.empty()) continue;
        out += code_part + "\n";
    }
    return out;
}

std::string PromptOptimizer::BuildMinimalToolSignature(const nlohmann::ordered_json& tool)
{
    // 仅保留参数名与是否必需（如 read(path, offset?, limit?)），
    // 不含类型/描述，用于压缩预算仍不足时的最终降级。
    if (!tool.contains("function") || !tool["function"].contains("name")) return "";
    std::string name = tool["function"]["name"];
    if (name.empty() || name == "image") return "";

    std::ostringstream sig;
    sig << name << "(";
    if (tool["function"].contains("parameters") && tool["function"]["parameters"].contains("properties")) {
        const auto& properties = tool["function"]["parameters"]["properties"];
        const auto& required = tool["function"]["parameters"].value("required", json::array());
        bool first = true;
        for (auto it = properties.begin(); it != properties.end(); ++it) {
            if (!first) sig << ", ";
            first = false;
            sig << it.key();
            bool is_required = std::find(required.begin(), required.end(), it.key()) != required.end();
            if (!is_required) sig << "?";
        }
    }
    sig << ")";
    return sig.str();
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
                // P1：按 CJK 感知 bytes/token 换算比例估算截断字节数，不再统一按 4:1
                // （max_chars 传给 safe_utf8_truncate，量纲是字节，与该函数返回值一致）
                const auto& fid_cfg = model_config_.GetPromptOptimizationConfig().fidelity;
                double bytes_per_token = EstimateCjkAwareBytesPerToken(
                    content, fid_cfg.cjk_bytes_per_token, fid_cfg.ascii_bytes_per_token);
                size_t max_chars = static_cast<size_t>(
                    static_cast<double>(config.max_section_tokens) * bytes_per_token);
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
