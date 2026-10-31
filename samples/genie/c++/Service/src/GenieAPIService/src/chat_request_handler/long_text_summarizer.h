//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef LONG_TEXT_SUMMARIZER_H
#define LONG_TEXT_SUMMARIZER_H

#include "../model/model_config.h"
#include "../model/model_instance_config.h"
#include "summary_cache.h"
#include <nlohmann/json.hpp>
#include <string>
#include <functional>
#include <vector>

using json = nlohmann::ordered_json;

class LongTextSummarizer
{
public:
    using InferFn = std::function<std::string(const std::string& prompt)>;

    using IsAliveFn = std::function<bool()>;

    LongTextSummarizer(
        const PromptOptimizationConfig::LongTextSummarizationConfig& config,
        const ModelInstanceConfig& instance_config,
        SummaryCache* cache,
        InferFn infer_fn,
        size_t context_size,
        IsAliveFn is_alive_fn = nullptr
    );

    bool ProcessMessages(json& messages);

private:
    int FindLastUserTextMessage(const json& messages) const;

    int FindTrailingToolChainStart(const json& messages) const;

    std::string Summarize(const std::string& content, const std::string& source_hint);

    std::string MapChunk(const std::string& chunk, int chunk_idx, int total_chunks,
                         const std::string& source_hint);

    std::string ReduceSummaries(const std::vector<std::string>& summaries,
                                const std::string& source_hint);

    std::string BuildMapPrompt(const std::string& chunk, int chunk_idx, int total_chunks,
                               const std::string& source_hint) const;

    std::string BuildReducePrompt(const std::string& combined_summaries,
                                  const std::string& source_hint) const;

    const PromptOptimizationConfig::LongTextSummarizationConfig& config_;
    const ModelInstanceConfig& instance_config_;
    SummaryCache* cache_;
    InferFn infer_fn_;
    IsAliveFn is_alive_fn_;
    size_t context_size_;
    size_t trigger_threshold_;
    size_t chunk_token_limit_;
};

#endif
