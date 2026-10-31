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

namespace {

constexpr const char* kVerificationFirstRuleText =
    "## Verification First\n"
    "当你对某个次要事实不确定时，优先调用可用工具（如 web_fetch、exec）验证；"
    "如果没有工具能验证，直接如实说明未确认并结束任务，不要重复表达同一个不确定的猜测。"
    "一次验证只核一件事，优先用已经确认过的具体路径直查，不要做全盘或多模式扫描，"
    "也不要把多条命令串联成一条。\n"
    "\n";

}

const std::string& PromptOptimizer::VerificationFirstRule()
{
    static const std::string rule(kVerificationFirstRuleText);
    return rule;
}

PromptOptimizer::PromptOptimizer(IModelConfig& config, ContextBase* context, int context_size)
    : model_config_(config), context_override_(context), context_size_(context_size)
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

    if (system_prompt.find("agent=main") != std::string::npos) {
        My_Log{My_Log::Level::kInfo} << "[AgentType] Detected: MAIN_AGENT (agent=main found in system prompt)" << std::endl;
        return AgentType::MAIN_AGENT;
    }

    My_Log{My_Log::Level::kInfo} << "[AgentType] Detected: SUBAGENT (agent=main not found)" << std::endl;
    return AgentType::SUBAGENT;
}

std::string PromptOptimizer::OptimizeSubagentSystemPrompt(
    const std::string& system_prompt,
    const nlohmann::ordered_json& request_data)
{
    try {
        last_stats_.original_tokens = CountTokens(system_prompt);

        IntentType detected_intent = IntentType::GENERAL_CHAT;
        std::string matched_skill;
        std::string optimized = BuildSystemContext(request_data, &detected_intent, &matched_skill);
        last_stats_.detected_intent = detected_intent;
        last_stats_.matched_skill = matched_skill;

        const PromptSectionsConfig& subagent_cfg = model_config_.GetSubagentPromptSectionsConfig();
        if (subagent_cfg.enabled) {
            std::vector<PromptSection> sections = ParseMarkdownSections(system_prompt);
            std::string appended = FilterSectionsByConfig(sections, subagent_cfg);
            if (!appended.empty()) {
                optimized += appended;
            }
        }

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

        IntentType detected_intent = IntentType::GENERAL_CHAT;
        std::string matched_skill;
        std::string optimized = BuildSystemContext(request_data, &detected_intent, &matched_skill);

        std::string filtered = AppendFilteredSections(system_prompt);
        if (!filtered.empty()) {
            optimized += "# Additional Context\n\n";
            optimized += filtered;
        }

        last_stats_.original_tokens = CountTokens(system_prompt);
        last_stats_.optimized_tokens = CountTokens(optimized);
        last_stats_.savings_percent =
            ComputeSavingsPercent(last_stats_.original_tokens, last_stats_.optimized_tokens);
        last_stats_.detected_intent = detected_intent;
        last_stats_.matched_skill = matched_skill;

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

    RuntimeSkillMappings runtime_skills = ExtractSkillsFromRequest(request_data);

    const auto& relevance_cfg = config.relevance_filter;
    const auto& disclosure_cfg = config.skill_disclosure;
    std::vector<std::string> relevance_keywords;
    RuntimeSkillMappings filtered_skills = runtime_skills;

    std::vector<ScoredSkill> leveled_skills;
    size_t skills_budget_tokens = ComputeRelevanceTokenBudget(BudgetPartitionKind::kSkills, &request_data);

    const bool use_leveled_disclosure =
        disclosure_cfg.enabled && config.skill_catalog_format == "structured";
    if (relevance_cfg.enabled) {
        relevance_keywords = BuildRelevanceKeywords(request_data);
    }
    if (!runtime_skills.empty()) {
        if (use_leveled_disclosure) {

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

    last_stats_.skills_total = runtime_skills.size();
    last_stats_.skills_kept = filtered_skills.size();
    last_stats_.skills_budget_tokens = skills_budget_tokens;

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

    if (se.identity_intro && !config.system_prompts.identity_intro.empty()) {
        oss << config.system_prompts.identity_intro;
    }

    if (se.verification_first_rule) {
        oss << VerificationFirstRule();
    }

    if (!filtered_skills.empty()) {

        if (se.skill_rule && !config.system_prompts.skill_rule.empty()) {
            oss << config.system_prompts.skill_rule;
        }

        if (se.tools_intro) {
            std::string tools_intro_str;
            if (!config.system_prompts.tools_intro.empty()) {

                tools_intro_str = FilterToolsIntroByRequest(config.system_prompts.tools_intro, request_data);
            } else {

                tools_intro_str = BuildDynamicToolsIntro(request_data);
            }
            if (!tools_intro_str.empty()) {
                oss << tools_intro_str;
            }
        }
    }

    const SystemContextConfig& ctx_cfg = model_config_.GetSystemContextConfig();
    for (const auto& sec : ctx_cfg.sections) {
        if (!sec.enabled) continue;

        if (sec.title.find("Tool Usage Guidelines") != std::string::npos ||
            sec.title.find("Examples") != std::string::npos) {
            continue;
        }

        if (!sec.title.empty()) oss << sec.title << "\n";
        for (const auto& line : sec.lines) oss << line << "\n";
        if (!sec.title.empty() || !sec.lines.empty()) oss << "\n";
    }

    {
        SkillMappings name_to_path;
        for (const auto& [name, info] : runtime_skills) {
            if (!info.path.empty()) {
                name_to_path[name] = info.path;
            }
        }
        last_stats_.runtime_skill_mappings = std::move(name_to_path);
    }

    if (!filtered_skills.empty()) {

        const auto& opt_config = model_config_.GetPromptOptimizationConfig();
        std::string skill_section;
        if (use_leveled_disclosure && !leveled_skills.empty()) {

            skill_section = BuildLeveledSkillCatalog(leveled_skills);
        } else {
            skill_section = BuildStructuredSkillCatalog(filtered_skills);
        }
        if (!skill_section.empty()) {
            oss << skill_section;
        }

        if (opt_config.system_prompts.few_shot_examples_enabled.enabled) {
            std::string few_shot = BuildFewShotExamples(filtered_skills);
            if (!few_shot.empty()) {
                oss << few_shot;
            }
        } else {
            My_Log{My_Log::Level::kInfo} << "[BuildSystemContext] few_shot_examples_enabled.enabled=false, skipping few-shot examples" << std::endl;
        }
    }

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

    if (context_override_) {
        return context_override_->TokenLength(text);
    }
    auto handle = model_config_.get_genie_model_handle().lock();
    if (handle) {
        return handle->TokenLength(text);
    }

    return text.length() / 4;
}

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

        if (!name.empty() && !location.empty()) {
            SkillInfo info;
            info.name    = name;
            info.path    = location;
            info.use_for = desc;
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

    if (request_data.contains("messages") && request_data["messages"].is_array()) {
        for (const auto& msg : request_data["messages"]) {
            if (!msg.contains("role") || !msg["role"].is_string()) continue;
            if (msg["role"].get<std::string>() != "system") continue;

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
            break;
        }
    }

    return runtime_skills;
}

namespace {

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
    else { return {c, 1}; }

    if (pos + len > text.size()) {
        return {c, 1};
    }
    for (size_t i = 1; i < len; ++i) {
        unsigned char cc = static_cast<unsigned char>(text[pos + i]);
        if ((cc & 0xC0) != 0x80) {
            return {c, 1};
        }
        cp = (cp << 6) | (cc & 0x3F);
    }
    return {cp, len};
}

bool IsCjkPunctuationOrSpace(uint32_t cp) {
    return (cp >= 0x2000 && cp <= 0x206F) ||
           (cp >= 0x3000 && cp <= 0x303F) ||
           (cp >= 0xFF00 && cp <= 0xFFEF);
}

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

bool AliasMemberHit(const std::string& lower_kw, const std::string& member) {
    if (lower_kw == member) return true;
    if (lower_kw.size() >= 2 && member.find(lower_kw) != std::string::npos) return true;
    if (member.size() >= 3 && lower_kw.find(member) != std::string::npos) return true;
    return false;
}

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

std::string CollectSkillSignalLines(const std::string& description,
                                    std::vector<std::string>* out_lines) {
    static const std::vector<std::string> kPrefixes = {
        "tags:", "tag:", "keywords:", "triggers:", "trigger:", "use when:", "used when:",
        "entry:", "cmd:",
        "标签:", "标签：", "触发:", "触发：", "触发条件:", "触发条件：", "适用于:", "适用于：",
        "入口:", "入口：", "命令:", "命令：",
    };
    std::string values;
    std::istringstream iss(description);
    std::string line;
    while (std::getline(iss, line)) {

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

}

std::vector<std::string> PromptOptimizer::BuildRelevanceKeywords(
    const nlohmann::ordered_json& request_data) const
{
    std::vector<std::string> keywords;
    if (!request_data.contains("messages") || !request_data["messages"].is_array()) {
        return keywords;
    }

    const auto& messages = request_data["messages"];
    const size_t recent_window = model_config_.GetPromptOptimizationConfig().recent_window;

    size_t non_system_count = 0;
    for (const auto& msg : messages) {
        if (!msg.is_object()) continue;
        if (get_json_value<std::string>(msg, "role", "") != "system") {
            ++non_system_count;
        }
    }
    size_t old_cutoff = (non_system_count > recent_window) ? (non_system_count - recent_window) : 0;

    const bool anchor_enabled =
        model_config_.GetPromptOptimizationConfig().relevance_filter.anchor_first_user_message;

    std::string corpus;
    std::string anchor_text;
    bool anchor_seen = false;
    bool anchor_in_window = false;
    size_t window_users = 0;
    size_t non_system_idx = 0;
    for (const auto& msg : messages) {
        if (!msg.is_object()) continue;
        std::string role = get_json_value<std::string>(msg, "role", "");
        if (role == "system") continue;

        bool is_recent = (non_system_idx >= old_cutoff);
        ++non_system_idx;
        if (role != "user") continue;

        std::string text = SecurityUtils::ExtractMessageContentText(msg);
        if (!anchor_seen) {
            anchor_seen = true;
            anchor_text = text;
            anchor_in_window = is_recent;
        }
        if (!is_recent) continue;
        if (!text.empty()) {
            corpus += text;
            corpus += " ";
            ++window_users;
        }
    }

    const bool anchor_used =
        anchor_enabled && anchor_seen && !anchor_in_window && !anchor_text.empty();
    if (anchor_used) {
        corpus += anchor_text;
        corpus += " ";
    }

    if (corpus.empty()) {
        return keywords;
    }

    std::string lower_corpus = ToLower(corpus);

    std::unordered_set<std::string> seen;
    auto flush_token = [&](std::string& token) {
        if (!token.empty()) {
            if (seen.insert(token).second) {
                keywords.push_back(token);
            }
            token.clear();
        }
    };

    const bool cjk_bigram_enabled = model_config_.GetPromptOptimizationConfig().relevance_filter.cjk_bigram;

    std::string current_token;
    std::string prev_cjk_char;
    size_t i = 0;
    while (i < lower_corpus.size()) {
        auto [cp, len] = DecodeUtf8CodePoint(lower_corpus, i);
        if (cp < 0x80) {
            if (std::isalnum(static_cast<int>(cp))) {
                current_token += static_cast<char>(cp);
            } else {
                flush_token(current_token);
            }
            prev_cjk_char.clear();
        } else if (IsCjkPunctuationOrSpace(cp)) {
            flush_token(current_token);
            prev_cjk_char.clear();
        } else {

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

    if (model_config_.GetPromptOptimizationConfig().relevance_filter.intent_aliases_enabled) {
        size_t before = keywords.size();
        ExpandIntentAliases(keywords);
        My_Log{My_Log::Level::kInfo} << "[BuildRelevanceKeywords] intent alias expansion: "
                                      << before << " -> " << keywords.size() << " keyword(s)" << std::endl;
    }

    My_Log{My_Log::Level::kInfo} << "[BuildRelevanceKeywords] Extracted " << keywords.size()
                                  << " keyword(s) from recent_window=" << recent_window
                                  << " user message(s) (window_users=" << window_users
                                  << ", anchor=" << (anchor_used ? 1 : 0) << ")" << std::endl;
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
                break;
            }
        }
    }

    if (!description.empty()) {
        std::string lower_desc = ToLower(description);
        for (const auto& kw : keywords) {
            if (!IsEligibleRelevanceKeyword(kw)) continue;
            if (lower_desc.find(kw) != std::string::npos) {
                score += relevance_cfg.description_keyword_weight;
            }
        }
    }

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

        My_Log{My_Log::Level::kInfo} << "[FilterSkillsByRelevance] Empty keywords, bypassing filter ("
                                      << all_skills.size() << " skill(s) kept as-is)" << std::endl;
        return all_skills;
    }

    RuntimeSkillMappings filtered;

    std::vector<std::pair<std::string, size_t>> scored;
    scored.reserve(all_skills.size());
    for (const auto& [name, info] : all_skills) {

        std::string tags = CollectSkillSignalLines(info.use_for, nullptr);
        size_t score = ScoreRelevance(name, info.use_for, keywords, tags);
        if (score > 0) {
            scored.emplace_back(name, score);
        }
    }

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

    std::sort(scored.begin(), scored.end(), [](const auto& a, const auto& b) {
        if (a.second != b.second) return a.second > b.second;
        return a.first < b.first;
    });

    size_t used_tokens = 0;
    for (const auto& entry : scored) {
        const std::string& name = entry.first;
        const auto& info = all_skills.at(name);

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

    std::sort(candidates.begin(), candidates.end(), [](const ScoredSkill& a, const ScoredSkill& b) {
        if (a.score != b.score) return a.score > b.score;
        return a.name < b.name;
    });

    size_t effective_l2_top_k = disclosure_cfg.l2_top_k;
    if (disclosure_cfg.tie_aware_l2 && !keywords.empty() && effective_l2_top_k > 0 &&
        effective_l2_top_k < candidates.size()) {
        size_t boundary_score = candidates[effective_l2_top_k - 1].score;
        while (effective_l2_top_k < candidates.size() &&
               candidates[effective_l2_top_k].score == boundary_score) {
            ++effective_l2_top_k;
        }
    }

    size_t used_tokens = 0;
    size_t dropped = 0;
    size_t zero_score_l0 = 0;
    for (size_t idx = 0; idx < candidates.size(); ++idx) {
        ScoredSkill entry = *(candidates.data() + idx);
        SkillDetailLevel desired = SkillDetailLevel::kNameOnly;
        if (entry.score > 0 || keywords.empty()) {
            if (idx < effective_l2_top_k) {
                desired = SkillDetailLevel::kFull;
            } else if (idx < effective_l2_top_k + disclosure_cfg.l1_top_k) {
                desired = SkillDetailLevel::kSummary;
            }
        } else {
            ++zero_score_l0;
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
                break;
            }
        }
        if (!placed) {

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
                                  << ", zero_score_l0=" << zero_score_l0
                                  << ", empty_keywords=" << (keywords.empty() ? 1 : 0)
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

            oss << "Path: " << skill.location << "\n";
            if (!skill.description.empty()) {
                oss << "Use for: " << skill.description << "\n";
            }
            oss << "\n";
            break;
        case SkillDetailLevel::kSummary: {

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

        My_Log{My_Log::Level::kInfo} << "[FilterToolsByRelevance] Empty keywords, bypassing filter ("
                                      << tools_array.size() << " tool(s) kept as-is)" << std::endl;
        return tools_array;
    }

    nlohmann::ordered_json filtered = nlohmann::ordered_json::array();
    const auto& relevance_cfg = model_config_.GetPromptOptimizationConfig().relevance_filter;
    if (relevance_cfg.keep_all_tools_ratio > 0.0) {
        const size_t all_tokens = CountTokens(tools_array.dump());
        const size_t keep_all_cap = static_cast<size_t>(
            static_cast<double>(context_size_) * relevance_cfg.keep_all_tools_ratio);
        if (all_tokens <= keep_all_cap && all_tokens <= token_budget) {
            My_Log{My_Log::Level::kInfo} << "[FilterToolsByRelevance] " << tools_array.size()
                                          << " tool(s) total " << all_tokens << " tok <= keep_all cap "
                                          << keep_all_cap << ", kept as-is" << std::endl;
            return tools_array;
        }
    }

    std::vector<std::pair<size_t, size_t>> scored;
    std::vector<size_t> protected_zero;
    scored.reserve(tools_array.size());
    for (size_t idx = 0; idx < tools_array.size(); ++idx) {
        const auto& tool = tools_array[idx];
        if (!tool.contains("function") || !tool["function"].contains("name")) continue;

        if (!tool["function"]["name"].is_string()) continue;
        std::string name = tool["function"]["name"].get<std::string>();

        std::string description;
        if (tool["function"].contains("description") && tool["function"]["description"].is_string()) {
            description = tool["function"]["description"].get<std::string>();
        }
        size_t score = ScoreRelevance(name, description, keywords);
        if (score > 0) {
            scored.emplace_back(idx, score);
        } else if (relevance_cfg.protect_core_capability_tools) {
            for (const auto& kw : relevance_cfg.core_capability_keywords) {
                if (str_contains(name, kw)) {
                    protected_zero.push_back(idx);
                    break;
                }
            }
        }
    }

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

    std::stable_sort(scored.begin(), scored.end(), [](const auto& a, const auto& b) {
        return a.second > b.second;
    });

    std::vector<size_t> ordered;
    ordered.reserve(scored.size() + protected_zero.size());
    for (const auto& entry : scored) {
        ordered.push_back(entry.first);
    }
    for (size_t idx : protected_zero) {
        ordered.push_back(idx);
    }

    size_t used_tokens = 0;
    std::vector<size_t> kept;
    for (size_t idx : ordered) {
        const auto& tool = tools_array[idx];
        size_t item_tokens = CountTokens(tool.dump());
        if (used_tokens + item_tokens > token_budget) {
            break;
        }
        used_tokens += item_tokens;
        kept.push_back(idx);
    }
    std::sort(kept.begin(), kept.end());
    for (size_t idx : kept) {
        filtered.push_back(tools_array[idx]);
    }

    const size_t protected_kept =
        filtered.size() > scored.size() ? filtered.size() - scored.size() : 0;
    My_Log{My_Log::Level::kInfo} << "[FilterToolsByRelevance] " << tools_array.size()
                                  << " candidate(s) -> " << filtered.size()
                                  << " kept within token_budget=" << token_budget
                                  << " (scored=" << scored.size()
                                  << ", protected=" << protected_kept
                                  << "/" << protected_zero.size() << ")" << std::endl;
    return filtered;
}

bool PromptOptimizer::HasBudgetContention(const nlohmann::ordered_json& request_data) const
{

    bool has_tools = request_data.contains("tools") && request_data["tools"].is_array() &&
                     !request_data["tools"].empty();
    if (!has_tools) {
        return false;
    }

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

    const auto& config = model_config_.GetPromptOptimizationConfig();
    int total_context = context_size_;
    int reserved_output = static_cast<int>(total_context * config.output_reserve_ratio);
    size_t total_budget = static_cast<size_t>(std::max(total_context - reserved_output, 0));

    const auto& bp_cfg = config.budget_partition;
    if (!bp_cfg.enabled) {
        return total_budget;
    }

    if (request_data == nullptr || !HasBudgetContention(*request_data)) {
        return total_budget;
    }

    size_t skills_floor = static_cast<size_t>(static_cast<double>(total_budget) * bp_cfg.skills_floor_ratio);
    size_t tools_budget_cap = static_cast<size_t>(static_cast<double>(total_budget) * bp_cfg.tools_ratio);

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

            return tools_budget;
    }
}

std::string PromptOptimizer::BuildStructuredSkillCatalog(
    const RuntimeSkillMappings& runtime_skills
) const {
    std::ostringstream oss;

    const auto& config = model_config_.GetPromptOptimizationConfig();

    if (config.system_prompts.sections_enabled.catalog_structured_intro &&
        !config.system_prompts.catalog_structured_intro.empty()) {
        oss << config.system_prompts.catalog_structured_intro;
    }

    for (const auto& [skill_name, skill_info] : runtime_skills) {
        oss << "Path: " << skill_info.path << "\n";

        if (!skill_info.use_for.empty()) {
            oss << "Use for: " << skill_info.use_for << "\n";
        }
        oss << "\n";
    }

    return oss.str();
}

std::string PromptOptimizer::BuildFewShotExamples(const RuntimeSkillMappings& runtime_skills) const {

    if (runtime_skills.empty()) {
        return "";
    }

    const auto& sys_prompts = model_config_.GetPromptOptimizationConfig().system_prompts;
    const auto& fe = sys_prompts.few_shot_examples_enabled;

    bool any_enabled = fe.skill_correct_call || fe.no_skill_needed;
    if (!any_enabled) {
        My_Log{My_Log::Level::kInfo} << "[BuildFewShotExamples] All example types disabled, skipping" << std::endl;
        return "";
    }

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

    int example_idx = 1;
    const int max_skill = fe.max_skill_examples;
    if (fe.skill_correct_call && max_skill > 0) {
        for (const auto& [skill_name, skill_info] : runtime_skills) {
            if (example_idx > max_skill) break;

            oss << apply_idx(sys_prompts.few_shot_skill_title_template, example_idx);
            oss << "```\n";

            std::string user_query = skill_info.use_for;
            if (user_query.empty()) {
                user_query = sys_prompts.few_shot_default_user_query_prefix
                           + skill_name
                           + sys_prompts.few_shot_default_user_query_suffix;
            } else if (user_query.size() > 50) {

                size_t cut = user_query.rfind(' ', 50);
                if (cut == std::string::npos) cut = 50;

                user_query = safe_utf8_truncate(user_query, cut, "...");
            }

            oss << sys_prompts.few_shot_user_label << "\"" << user_query << "\"\n";

            oss << sys_prompts.few_shot_correct_call_label
                << "<tool_call>{\"name\": \"read\", \"arguments\": "
                << "{\"path\": \"" << escape_path(skill_info.path) << "\"}}</tool_call>\n";

            oss << "```\n\n";
            ++example_idx;
        }
    } else {
        My_Log{My_Log::Level::kInfo} << "[BuildFewShotExamples] skill_correct_call disabled, skipping skill examples" << std::endl;
    }

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

    auto wrap = [&](std::string descs) {
        while (!descs.empty() && descs.back() == '\n') descs.pop_back();
        return tool_prompt_template.empty() ? descs : str_replace(tool_prompt_template, "{tool_descs}", descs);
    };

    try {
        json tools_array = json::parse(tool_descriptions);

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

            int tier = 1;
            size_t tools_total = tools_array.size();
            size_t tools_kept_count = 0;

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

            if (CountTokens(result) > token_budget) {
                optimized_tools = StripToolCommentLines(optimized_tools);
                result = wrap(optimized_tools);
                tier = 2;

                My_Log{My_Log::Level::kInfo} << "[Optimizer] Tools still over budget after Tier1, stripped comments" << std::endl;
            }

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

        result = wrap(tool_descriptions);
        last_stats_.tools_tier = 0;
        last_stats_.tools_total = 0;
        last_stats_.tools_kept = 0;
    }

    return result;
}

const std::unordered_map<std::string, std::string>& PromptOptimizer::GetKnownToolSignatures()
{

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
    return kToolSignatures;
}

std::string PromptOptimizer::BuildDynamicToolsIntro(const nlohmann::ordered_json& request_data) const
{

    if (!request_data.contains("tools") || !request_data["tools"].is_array()
        || request_data["tools"].empty()) {
        return "";
    }

    json tools_array = request_data["tools"];
    const auto& relevance_cfg = model_config_.GetPromptOptimizationConfig().relevance_filter;
    if (relevance_cfg.enabled) {
        std::vector<std::string> keywords = BuildRelevanceKeywords(request_data);
        tools_array = FilterToolsByRelevance(tools_array, keywords,
                                             ComputeRelevanceTokenBudget(BudgetPartitionKind::kTools, &request_data));
    }
    if (tools_array.empty()) {
        return "";
    }

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

    const auto& kToolSignatures = GetKnownToolSignatures();

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

    if (!request_data.contains("tools") || !request_data["tools"].is_array()
        || request_data["tools"].empty()) {
        return tools_intro;
    }

    json relevant_tools = request_data["tools"];
    const auto& relevance_cfg = model_config_.GetPromptOptimizationConfig().relevance_filter;
    if (relevance_cfg.enabled) {
        std::vector<std::string> keywords = BuildRelevanceKeywords(request_data);
        relevant_tools = FilterToolsByRelevance(relevant_tools, keywords,
                                                ComputeRelevanceTokenBudget(BudgetPartitionKind::kTools, &request_data));
    }

    std::unordered_set<std::string> client_tools;
    for (const auto& tool : relevant_tools) {
        if (tool.contains("function") && tool["function"].contains("name")) {
            client_tools.insert(tool["function"]["name"].get<std::string>());
        }
    }

    std::istringstream iss(tools_intro);
    std::ostringstream oss;
    std::string line;
    int removed = 0;
    while (std::getline(iss, line)) {
        if (line.size() >= 2 && line[0] == '-' && line[1] == ' ') {

            std::string rest = line.substr(2);
            auto paren_pos = rest.find('(');
            auto space_pos = rest.find(' ');
            auto end_pos = std::min(paren_pos, space_pos);
            std::string tool_name = (end_pos != std::string::npos) ? rest.substr(0, end_pos) : rest;
            if (client_tools.count(tool_name) == 0) {
                ++removed;
                continue;
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

    return "";
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

    std::string trimmed = tool_response;
    trimmed = std::regex_replace(trimmed, std::regex(R"(^(\s*\n)+)"), "");
    trimmed = std::regex_replace(trimmed, std::regex(R"((\s*\n)+$)"), "");

    trimmed = InjectSpawnGuard(trimmed);

    return "<tool_response>\n" + trimmed + "\n</tool_response>\n";
}

std::string PromptOptimizer::OptimizeHarmonySystemMessage(
    const std::string& knowledge_cutoff,
    const std::string& current_date,
    const std::string& reasoning_level,
    bool has_tools)
{

    std::string result = "You are ChatGPT, a large language model trained by OpenAI.\n";
    result += "Knowledge cutoff: " + knowledge_cutoff + "\n";
    result += "Current date: " + current_date + "\n\n";

    result += "Reasoning: " + reasoning_level + "\n\n";

    result += "# Valid channels: analysis, commentary, final. Channel must be included for every message.";

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

    std::string result;

    result += "# System Context\n\n";
    result += BuildSystemContext(request_data);

    size_t system_context_tokens = CountTokens(result);

    size_t instructions_tokens = 0;
    const PromptSectionsConfig& sections_cfg = model_config_.GetPromptSectionsConfig();
    if (sections_cfg.enabled) {
        result += "# Instructions\n\n";

        std::string optimized_instructions = OptimizeInstructions(instructions);
        result += optimized_instructions;

        instructions_tokens = CountTokens(optimized_instructions);

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

    json relevant_tools = tools;
    const auto& relevance_cfg = model_config_.GetPromptOptimizationConfig().relevance_filter;
    if (relevance_cfg.enabled && !tools.is_null() && tools.is_array() && !tools.empty()) {
        std::vector<std::string> keywords = BuildRelevanceKeywords(request_data);
        relevant_tools = FilterToolsByRelevance(tools, keywords,
                                                ComputeRelevanceTokenBudget(BudgetPartitionKind::kTools, &request_data));
    }

    if (!relevant_tools.is_null() && !relevant_tools.empty()) {

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

    std::string result;
    size_t tool_count = 0;

    if (!tools.is_array()) {
        My_Log{My_Log::Level::kInfo} << "Tools is not an array, skipping conversion" << std::endl;
        return result;
    }

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

        std::string ts_def = GetOptimizedTypeScriptDefinition(tool_name);

        if (!ts_def.empty()) {
            result += ts_def + "\n\n";
            tool_count++;
            My_Log{My_Log::Level::kDebug} << "[Harmony] Using optimized TypeScript definition for: " << tool_name << std::endl;
        } else {

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

    std::string result;

    result = std::regex_replace(instructions, std::regex(R"(\n{3,})"), "\n\n");

    static const size_t kSummaryThreshold = 500;
    if (result.length() <= kSummaryThreshold) {
        return result;
    }

    std::string summarized;
    summarized.reserve(result.size() / 2);

    std::istringstream iss(result);
    std::string line;
    std::string current_paragraph;

    auto flush_paragraph = [&](const std::string& para) {
        if (!para.empty()) {

            if (para[0] == '#') {
                summarized += para + "\n\n";
            } else {

                bool handled_as_list = false;
                {

                    size_t first_line_end = para.find('\n');
                    const std::string& first_line = (first_line_end != std::string::npos)
                                                    ? para.substr(0, first_line_end)
                                                    : para;

                    size_t non_space = first_line.find_first_not_of(" \t");
                    if (non_space != std::string::npos) {
                        char c0 = first_line[non_space];
                        bool is_list = (c0 == '-' || c0 == '*');

                        if (!is_list && std::isdigit(static_cast<unsigned char>(c0))) {
                            size_t dot_pos = first_line.find('.', non_space + 1);
                            if (dot_pos != std::string::npos &&
                                dot_pos == non_space + (first_line.find_first_not_of("0123456789", non_space) - non_space)) {
                                is_list = true;
                            }
                        }
                        if (is_list) {
                            summarized += para + "\n\n";
                            handled_as_list = true;
                        }
                    }
                }

                if (!handled_as_list) {

                    size_t first_sentence_end = std::string::npos;
                    for (size_t k = 0; k < para.size(); k++) {
                        char c = para[k];
                        if (c == '.' || c == '!' || c == '?') {
                            first_sentence_end = k + 1;
                            break;
                        }
                        if (c == '\n') {
                            first_sentence_end = k;
                            break;
                        }
                    }

                    if (first_sentence_end != std::string::npos && first_sentence_end < para.size()) {

                        summarized += safe_utf8_truncate(para, first_sentence_end, "") + "\n\n";
                    } else {

                        summarized += para + "\n\n";
                    }
                }
            }
        }
    };

    while (std::getline(iss, line)) {
        if (line.empty()) {

            flush_paragraph(current_paragraph);
            current_paragraph.clear();
        } else {
            if (!current_paragraph.empty()) current_paragraph += '\n';
            current_paragraph += line;
        }
    }

    flush_paragraph(current_paragraph);

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

        My_Log{My_Log::Level::kDebug} << "[Harmony] Filtered out 'image' tool" << std::endl;
        return "";
    }

    return "";
}

std::string PromptOptimizer::GenerateBasicTypeScriptDefinition(const json& tool)
{

    try {
        std::string tool_name = tool["function"]["name"];
        std::string description = tool["function"].value("description", "");

        if (tool_name == "image") {
            return "";
        }

        std::string result;

        if (!description.empty()) {
            result += "// " + description + "\n";
        }

        if (tool["function"].contains("parameters") &&
            tool["function"]["parameters"].contains("properties") &&
            !tool["function"]["parameters"]["properties"].empty())
        {

            result += "type " + tool_name + " = (_: {\n";

            const auto& properties = tool["function"]["parameters"]["properties"];
            const auto& required = tool["function"]["parameters"].value("required", json::array());

            for (auto it = properties.begin(); it != properties.end(); ++it) {
                std::string param_name = it.key();
                const auto& param_def = it.value();

                bool is_required = std::find(required.begin(), required.end(), param_name) != required.end();

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

                std::string param_desc = param_def.value("description", "");

                result += "  " + param_name;
                if (!is_required) {
                    result += "?";
                }
                result += ": " + param_type + ",";

                if (!param_desc.empty()) {
                    if (param_desc.length() > 60) {

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

    std::vector<PromptSection> sections;

    std::istringstream iss(prompt);
    std::string line;

    std::string current_content;
    int current_level = 0;
    std::string current_title;

    auto flush_section = [&]() {
        if (!(current_level == 0 && current_content.empty())) {

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
        }
    };

    while (std::getline(iss, line)) {

        int level = 0;
        if (!line.empty() && line[0] == '#') {
            size_t i = 0;
            while (i < line.size() && line[i] == '#') { ++i; }
            if (i <= 3 && i < line.size() && line[i] == ' ') {
                level = static_cast<int>(i);
            }
        }

        if (level > 0) {

            flush_section();

            current_level = level;

            current_title = line.substr(level + 1);

            size_t ts = current_title.find_first_not_of(" \t");
            size_t te = current_title.find_last_not_of(" \t\r");
            if (ts != std::string::npos) {
                current_title = current_title.substr(ts, te - ts + 1);
            } else {
                current_title.clear();
            }
            current_content = line + "\n";
        } else {

            current_content += line + "\n";
        }
    }

    flush_section();

    My_Log{My_Log::Level::kDebug} << "[SectionFilter] Parsed " << sections.size()
                                   << " sections from prompt (" << prompt.size() << " bytes)" << std::endl;
    return sections;
}

bool PromptOptimizer::ShouldIncludeSection(
    const PromptSection& section,
    const PromptSectionsConfig& config)
{

    std::string title_lower = ToLower(section.title);

    for (const auto& rule : config.rules) {

        if (rule.heading_level != 0 && rule.heading_level != section.heading_level) {
            continue;
        }

        if (!rule.title_contains.empty()) {
            std::string keyword_lower = ToLower(rule.title_contains);
            if (title_lower.find(keyword_lower) == std::string::npos) {
                continue;
            }
        }

        My_Log{My_Log::Level::kDebug} << "[SectionFilter] Rule matched: title='" << section.title
                                       << "' (level=" << section.heading_level
                                       << ") -> " << (rule.include ? "include" : "exclude") << std::endl;
        return rule.include;
    }

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

        if (section.heading_level == 0) {
            My_Log{My_Log::Level::kDebug} << "[SectionFilter] Skipping preamble section (level=0)" << std::endl;
            continue;
        }

        bool should_include = ShouldIncludeSection(section, config);

        if (!should_include) {
            ++excluded_count;
            continue;
        }

        std::string content = section.full_content;

        if (config.max_section_tokens > 0) {
            size_t token_count = CountTokens(content);
            if (token_count > static_cast<size_t>(config.max_section_tokens)) {

                const auto& fid_cfg = model_config_.GetPromptOptimizationConfig().fidelity;
                double bytes_per_token = EstimateCjkAwareBytesPerToken(
                    content, fid_cfg.cjk_bytes_per_token, fid_cfg.ascii_bytes_per_token);
                size_t max_chars = static_cast<size_t>(
                    static_cast<double>(config.max_section_tokens) * bytes_per_token);
                if (content.size() > max_chars) {

                    content = safe_utf8_truncate(content, max_chars, "\n...[truncated]\n");
                    My_Log{My_Log::Level::kDebug} << "[SectionFilter] Section '" << section.title
                                                   << "' truncated to " << max_chars << " chars"
                                                   << " (max_section_tokens=" << config.max_section_tokens << ")" << std::endl;
                }
            }
        }

        result += content;
        if (!result.empty() && result.back() != '\n') result += '\n';
        result += '\n';
        ++included_count;
    }

    My_Log{My_Log::Level::kInfo} << "[SectionFilter] Filtered sections: included=" << included_count
                                  << ", excluded=" << excluded_count
                                  << ", output_size=" << result.size() << " bytes" << std::endl;
    return result;
}
