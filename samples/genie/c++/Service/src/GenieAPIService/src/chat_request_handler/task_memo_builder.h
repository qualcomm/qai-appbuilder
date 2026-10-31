//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef TASK_MEMO_BUILDER_H
#define TASK_MEMO_BUILDER_H

#include "../model/model_config.h"
#include "../model/model_instance_config.h"
#include "../chat_history/chat_history.h"
#include "task_memo_store.h"
#include "tool_call_repetition_store.h"
#include <nlohmann/json.hpp>
#include <string>
#include <vector>
#include <functional>
#include <optional>
#include <utility>

using json = nlohmann::ordered_json;

class TaskMemoBuilder
{
public:
    using InferFn = std::function<std::string(const std::string& prompt)>;
    using IsAliveFn = std::function<bool()>;

    struct UpdateResult {
        bool active = false;
        double confidence = 0.0;
        size_t refresh_count = 0;
        size_t pages_total = 0;
        std::string compact_render;
    };

    struct RepeatDetection {
        std::string command;
        std::string result_content;
    };

    TaskMemoBuilder(
        const PromptOptimizationConfig::TaskMemoConfig& config,
        const ModelInstanceConfig& instance_config,
        TaskMemoStore* store,
        InferFn infer_fn,
        IsAliveFn is_alive_fn = nullptr
    );

    static std::string ComputeFingerprint(const json& messages);
    static std::string ComputeFirstMsgKey(const json& messages);
    static uint64_t ComputeScopeHash(const json& messages, const json* request_data = nullptr);
    static std::string StripThinkBlock(const std::string& content);
    static std::string ExtractThinkConclusion(const std::string& content);
    static bool IsReasoningNoteMimicry(const std::string& content);
    static std::string ExtractToolCallSummary(const std::string& content);
    static void PinThinkConclusion(const std::string& session_key, const std::string& call_signature,
                                   const std::string& conclusion);
    static std::string LookupPinnedConclusion(const std::string& session_key, const std::string& call_signature);

    static std::string ExtractReadToolPath(const std::string& assistant_content);
    static bool LooksLikeToolFailure(const std::string& content);
    static std::string FirstFailureLine(const std::string& content);

    std::optional<json> Lookup(const json& raw_messages) const;

    UpdateResult Update(const json& raw_messages, const std::vector<GenieChatMessage>& dropped_messages);

    void RecordRepeatDetections(const std::vector<RepeatDetection>& detections);

    static std::string Render(const json& entry, size_t max_chars = 0);
    static std::string RenderCompact(const json& entry);

    static std::string RenderRedundantToolCallNote(int repeat_count, const std::string& outcome_preview = "",
                                                    bool digest_stable = true, int hard_block_threshold = 0);
    static std::string RenderNonContiguousRepeatNote(int repeat_count, const std::string& outcome_preview = "",
                                                      bool digest_stable = true, int hard_block_threshold = 0);

    static std::string BuildRereadFromTopNote(const std::string& read_ref, const json& read_files,
                                               std::string& path_out);

    static bool DetectOverlapReread(const json& file_entry, size_t first, size_t last,
                                     size_t& overlap_first, size_t& overlap_last);

    static bool DetectRecentOverlapReread(const json& file_entry, size_t first, size_t last,
                                           size_t current_seq, size_t recency_window,
                                           size_t& overlap_first, size_t& overlap_last);

    static std::string BuildOverlapRereadNote(int overlap_count, int overlap_threshold,
                                               const std::string& path, const std::string& range,
                                               size_t next_offset = 0);

private:
    json BuildRuleLayer(const json& prev, const std::vector<GenieChatMessage>& dropped_messages,
                        const std::vector<std::string>& dropped_tool_call_content) const;
    bool ShouldTriggerModelLayer(const std::vector<GenieChatMessage>& dropped_messages,
                                  bool has_prev, double prev_confidence) const;
    std::string BuildModelPrompt(const json& prev, const json& rule_layer_result) const;
    bool TryParseModelOutput(const std::string& raw, json& out) const;

    std::pair<std::string, bool> ExtractGoalAnchor(const json& raw_messages) const;

    const PromptOptimizationConfig::TaskMemoConfig& config_;
    const ModelInstanceConfig& instance_config_;
    TaskMemoStore* store_;
    InferFn infer_fn_;
    IsAliveFn is_alive_fn_;

    std::vector<RepeatDetection> pending_repeat_detections_;
};

#endif
