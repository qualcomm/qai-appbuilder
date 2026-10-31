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
#include <unordered_map>
#include <vector>
#include <optional>
#include "../model/model_config.h"
#include "../chat_history/chat_history.h"
#include "prompt_stats_helper.h"
#include "message_pre_filter.h"

enum class IntentType {
    TOOL_CALL,
    SKILL_QUERY,
    GENERAL_CHAT
};

enum class AgentType {
    MAIN_AGENT,
    SUBAGENT,
};

enum class BudgetPartitionKind {
    kSkills,
    kTools
};

enum class SkillDetailLevel {
    kNameOnly,
    kSummary,
    kFull
};

struct ScoredSkill {
    std::string name;
    std::string description;
    std::string location;
    std::string tags;
    size_t score = 0;
    SkillDetailLevel level = SkillDetailLevel::kFull;
};

class PromptOptimizer {
public:

    explicit PromptOptimizer(IModelConfig& config, ContextBase* context = nullptr,
                             int context_size = DEFAULT_CONTEXT_SIZE);

    static const std::string& VerificationFirstRule();

    AgentType DetectAgentType(const std::string& system_prompt);

    std::string OptimizeSystemPrompt(
        const std::string& system_prompt,
        const nlohmann::ordered_json& request_data = nlohmann::ordered_json::object()
    );

    std::string OptimizeSubagentSystemPrompt(
        const std::string& system_prompt,
        const nlohmann::ordered_json& request_data = nlohmann::ordered_json::object()
    );

    std::string OptimizeToolsPrompt(
        const std::string& tool_descriptions,
        const std::string& tool_prompt_template,
        size_t token_budget,
        const nlohmann::ordered_json& request_data = nlohmann::ordered_json::object()
    );

    std::vector<std::string> ConvertOpenAIToolCalls(
        const nlohmann::ordered_json& tool_calls_array
    );

    std::string OptimizeToolResponse(
        const std::string& tool_response
    );

    std::string InjectSpawnGuard(const std::string& tool_response_content) const;

    std::string OptimizeHarmonySystemMessage(
        const std::string& knowledge_cutoff,
        const std::string& current_date,
        const std::string& reasoning_level,
        bool has_tools
    );

    std::string OptimizeHarmonyDeveloperMessage(
        const std::string& instructions,
        const nlohmann::ordered_json& tools,
        const nlohmann::ordered_json& request_data = nlohmann::ordered_json::object()
    );

    std::string ConvertToolsToOptimizedTypeScript(
        const nlohmann::ordered_json& tools,
        const nlohmann::ordered_json& request_data = nlohmann::ordered_json::object()
    );

    struct OptimizationStats {
        size_t original_tokens;
        size_t optimized_tokens;
        float savings_percent;
        IntentType detected_intent;
        std::string matched_skill;

        int tools_tier = 0;
        size_t tools_total = 0;
        size_t tools_kept = 0;
        size_t skills_total = 0;
        size_t skills_kept = 0;

        size_t skills_budget_tokens = 0;

        size_t skills_l2 = 0;
        size_t skills_l1 = 0;
        size_t skills_l0 = 0;

        SkillMappings runtime_skill_mappings;
    };

    OptimizationStats GetLastStats() const { return last_stats_; }

    void SetContext(ContextBase* context) { context_override_ = context; }

    static const std::unordered_map<std::string, std::string>& GetKnownToolSignatures();

private:
    IModelConfig& model_config_;
    ContextBase* context_override_{nullptr};
    int context_size_;
    OptimizationStats last_stats_;

    size_t CountTokens(const std::string& text) const;

    RuntimeSkillMappings ExtractSkillsFromRequest(const nlohmann::ordered_json& request_data) const;

    std::string BuildStructuredSkillCatalog(const RuntimeSkillMappings& runtime_skills) const;

    std::string BuildFewShotExamples(const RuntimeSkillMappings& runtime_skills) const;

    std::string GetOptimizedToolDefinition(const std::string& tool_name);

    std::string BuildDynamicToolsIntro(const nlohmann::ordered_json& request_data) const;

    std::string FilterToolsIntroByRequest(const std::string& tools_intro,
                                          const nlohmann::ordered_json& request_data) const;

    std::vector<std::string> BuildRelevanceKeywords(const nlohmann::ordered_json& request_data) const;

    size_t ScoreRelevance(const std::string& name, const std::string& description,
                          const std::vector<std::string>& keywords,
                          const std::string& tags = std::string()) const;

    RuntimeSkillMappings FilterSkillsByRelevance(const RuntimeSkillMappings& all_skills,
                                                 const std::vector<std::string>& keywords,
                                                 size_t token_budget) const;

    std::vector<ScoredSkill> AssignSkillDetailLevels(const RuntimeSkillMappings& all_skills,
                                                     const std::vector<std::string>& keywords,
                                                     size_t skills_token_budget) const;

    std::string RenderSkillEntry(const ScoredSkill& skill) const;

    std::string BuildLeveledSkillCatalog(const std::vector<ScoredSkill>& skills) const;

    nlohmann::ordered_json FilterToolsByRelevance(const nlohmann::ordered_json& tools_array,
                                                  const std::vector<std::string>& keywords,
                                                  size_t token_budget) const;

    size_t ComputeRelevanceTokenBudget(BudgetPartitionKind kind = BudgetPartitionKind::kTools,
                                       const nlohmann::ordered_json* request_data = nullptr) const;

    bool HasBudgetContention(const nlohmann::ordered_json& request_data) const;

    std::string BuildSystemContext(const nlohmann::ordered_json& request_data,
                                   IntentType* out_intent = nullptr,
                                   std::string* out_matched_skill = nullptr);

    std::string OptimizeInstructions(const std::string& instructions);

    std::string GetOptimizedTypeScriptDefinition(const std::string& tool_name);

    std::string GenerateBasicTypeScriptDefinition(const nlohmann::ordered_json& tool);

    std::string StripToolCommentLines(const std::string& text);

    std::string BuildMinimalToolSignature(const nlohmann::ordered_json& tool);

    std::string AppendFilteredSections(const std::string& source_prompt);

    struct PromptSection {
        int heading_level;
        std::string title;
        std::string full_content;
    };

    std::vector<PromptSection> ParseMarkdownSections(const std::string& prompt);

    std::string FilterSectionsByConfig(
        const std::vector<PromptSection>& sections,
        const PromptSectionsConfig& config
    );

    bool ShouldIncludeSection(
        const PromptSection& section,
        const PromptSectionsConfig& config
    );

    static std::string ToLower(const std::string& s);

    static float ComputeSavingsPercent(size_t original_tokens, size_t optimized_tokens);
};

#endif
