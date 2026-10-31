//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef TASK_COMPLEXITY_EVALUATOR_H
#define TASK_COMPLEXITY_EVALUATOR_H

#include "content_security_types.h"
#include "../../model/model_config.h"
#include <string>
#include <unordered_map>
#include <list>
#include <mutex>

class TaskComplexityEvaluator
{
public:
    explicit TaskComplexityEvaluator(const RoutingConfig::ComplexityConfig &config,
                                      IModelConfig *model_config = nullptr);

    ComplexityResult Evaluate(const json &messages,
                               const json &tools,
                               const InspectionContext &ctx);

private:
    ComplexityResult HeuristicEvaluate(const json &messages, const json &tools);

    int EstimateTokenCount(const json &messages, const json &tools);

    int CountToolCallsAfterLastUser(const json &messages);

    ComplexityLevel CheckComplexKeywords(const json &messages);

    std::string ExtractAllText(const json &messages);

    std::string ExtractLastUserMessage(const json &messages);

    bool LocalModelEvaluate(const std::string &task_summary,
                             int timeout_ms,
                             ComplexityLevel &result_level,
                             std::string &reason);

    std::string ExtractTaskSummary(const json &messages,
                                   const json &tools) const;

    static std::string ComputeHash(const std::string &text);

    struct CacheEntry {
        ComplexityLevel level;
        std::string reason;
    };

    bool CacheLookup(const std::string &key, CacheEntry &entry);
    void CacheInsert(const std::string &key, const CacheEntry &entry);

    RoutingConfig::ComplexityConfig config_;
    IModelConfig *model_config_;

    static constexpr size_t MAX_CACHE_SIZE = 64;
    mutable std::mutex cache_mutex_;
    std::list<std::pair<std::string, CacheEntry>> cache_list_;
    std::unordered_map<std::string, std::list<std::pair<std::string, CacheEntry>>::iterator> cache_map_;
};

#endif
