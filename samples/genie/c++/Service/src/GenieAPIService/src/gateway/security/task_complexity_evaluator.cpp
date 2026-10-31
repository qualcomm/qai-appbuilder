//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "task_complexity_evaluator.h"
#include "../../context/context_base.h"
#include "security_prompt_builder.h"
#include "security_utils.h"
#include "log.h"
#include "../../../../common/utils.h"
#include <algorithm>
#include <sstream>
#include <cctype>
#include <future>
#include <chrono>
#include <thread>
#include <atomic>

TaskComplexityEvaluator::TaskComplexityEvaluator(
        const RoutingConfig::ComplexityConfig &config,
        IModelConfig *model_config)
        : config_(config), model_config_(model_config)
{
}

std::string TaskComplexityEvaluator::ExtractAllText(const json &messages)
{
    std::string all_text;
    if (!messages.is_array())
        return all_text;

    for (const auto &msg : messages)
    {
        if (msg.contains("content"))
        {
            if (msg["content"].is_string())
            {
                all_text += msg["content"].get<std::string>() + " ";
            }
            else if (msg["content"].is_array())
            {
                for (const auto &part : msg["content"])
                {
                    if (part.contains("type") && part["type"] == "text" &&
                        part.contains("text") && part["text"].is_string())
                    {
                        all_text += part["text"].get<std::string>() + " ";
                    }
                }
            }
        }
    }
    return all_text;
}

std::string TaskComplexityEvaluator::ExtractLastUserMessage(const json &messages)
{
    if (!messages.is_array())
        return "";

    for (int i = static_cast<int>(messages.size()) - 1; i >= 0; --i)
    {
        const auto &msg = messages[i];
        if (!msg.contains("role") || !msg["role"].is_string())
            continue;
        if (msg["role"].get<std::string>() != "user")
            continue;

        std::string text;
        if (msg.contains("content"))
        {
            if (msg["content"].is_string())
            {
                text = msg["content"].get<std::string>();
            }
            else if (msg["content"].is_array())
            {
                for (const auto &part : msg["content"])
                {
                    if (part.contains("type") && part["type"] == "text" &&
                        part.contains("text") && part["text"].is_string())
                    {
                        text += part["text"].get<std::string>() + " ";
                    }
                }
            }
        }
        return text;
    }
    return "";
}

int TaskComplexityEvaluator::EstimateTokenCount(const json &messages, const json &tools)
{

    std::string all_text = ExtractAllText(messages);
    if (tools.is_array())
    {
        all_text += tools.dump();
    }

    if (model_config_)
    {

        auto handle = model_config_->GetDefaultModelHandle().lock();
        if (handle)
        {

            return static_cast<int>(handle->TokenLength(all_text));
        }
    }

    return static_cast<int>(all_text.size() / 3);
}

int TaskComplexityEvaluator::CountToolCallsAfterLastUser(const json &messages)
{
    int count = 0;
    if (!messages.is_array())
        return count;

    int last_user_idx = -1;
    for (int i = static_cast<int>(messages.size()) - 1; i >= 0; --i)
    {
        if (messages[i].contains("role") && messages[i]["role"].is_string())
        {
            if (messages[i]["role"].get<std::string>() == "user")
            {
                last_user_idx = i;
                break;
            }
        }
    }

    int start_idx = (last_user_idx >= 0) ? last_user_idx : 0;
    for (int i = start_idx; i < static_cast<int>(messages.size()); ++i)
    {
        if (messages[i].contains("tool_calls") && messages[i]["tool_calls"].is_array())
        {
            count += static_cast<int>(messages[i]["tool_calls"].size());
        }
    }
    return count;
}

ComplexityLevel TaskComplexityEvaluator::CheckComplexKeywords(const json &messages)
{

    const std::vector<std::string> &complex_keywords_c2 = config_.keywords_c2;
    const std::vector<std::string> &complex_keywords_c1 = config_.keywords_c1;

    std::string all_text = ExtractAllText(messages);

    std::string lower_text = all_text;
    std::transform(lower_text.begin(), lower_text.end(), lower_text.begin(),
                   [](unsigned char c) { return std::tolower(c); });

    for (const auto &kw : complex_keywords_c2)
    {

        if (lower_text.find(kw) != std::string::npos)
        {
            return ComplexityLevel::C2;
        }
    }

    for (const auto &kw : complex_keywords_c1)
    {

        if (lower_text.find(kw) != std::string::npos)
        {
            return ComplexityLevel::C1;
        }
    }

    return ComplexityLevel::C0;
}

ComplexityResult TaskComplexityEvaluator::HeuristicEvaluate(
        const json &messages, const json &tools)
{
    ComplexityResult result;
    result.complexity_level = ComplexityLevel::C0;

    int tool_call_count = CountToolCallsAfterLastUser(messages);
    int tool_calls_threshold = config_.thresholds.tool_calls;

    json last_user_only = json::array();
    std::string last_user_text = ExtractLastUserMessage(messages);
    if (!last_user_text.empty())
    {
        json last_user_msg = json::object();
        last_user_msg["role"] = "user";
        last_user_msg["content"] = last_user_text;
        last_user_only.push_back(last_user_msg);
    }

    ComplexityLevel keyword_level = CheckComplexKeywords(last_user_only);
    if (keyword_level == ComplexityLevel::C2)
    {
        result.complexity_level = ComplexityLevel::C2;
        result.reason = "C2 complex keywords detected in latest user message (e.g. generate report, code refactor)";
        return result;
    }

    if (tool_call_count > tool_calls_threshold)
    {
        result.complexity_level = ComplexityLevel::C1;
        result.reason = "tool_call_count_after_last_user=" + std::to_string(tool_call_count) +
                        " > complexity.thresholds.tool_calls=" + std::to_string(tool_calls_threshold);
        return result;
    }

    if (keyword_level == ComplexityLevel::C1)
    {
        result.complexity_level = ComplexityLevel::C1;
        result.reason = "C1 complex keywords detected in latest user message (e.g. analyze, summarize, design)";
        return result;
    }

    result.complexity_level = ComplexityLevel::C0;
    result.reason = "simple task: tool_calls_after_user=" + std::to_string(tool_call_count);
    return result;
}

std::string TaskComplexityEvaluator::ExtractTaskSummary(
        const json &messages,
        const json &tools) const
{

    auto extract_msg_text = [](const json& msg) -> std::string {
        if (!msg.contains("content")) return "";
        std::string text;
        const auto& content = msg["content"];
        if (content.is_string()) {
            text = content.get<std::string>();
        } else if (content.is_array()) {
            for (const auto& part : content) {
                if (part.contains("type") && part["type"] == "text" &&
                    part.contains("text") && part["text"].is_string()) {
                    text += part["text"].get<std::string>() + " ";
                }
            }
        }
        return SecurityUtils::CleanMessageText(text);
    };

    if (!messages.is_array() || messages.empty()) return "";

    std::shared_ptr<ContextBase> handle;
    size_t context_size = 0;
    size_t max_input_tokens = 0;
    if (model_config_) {
        auto h = model_config_->GetDefaultModelHandle().lock();
        if (h) {
            handle = h;
            const auto *default_instance_config = model_config_->GetDefaultInstanceConfig();
            int ctx_size = default_instance_config ? default_instance_config->get_context_size() : 0;
            context_size = (ctx_size > 0) ? static_cast<size_t>(ctx_size) : 8192;

            max_input_tokens = context_size * 4 / 5;
        }
    }

    if (handle && max_input_tokens > 0) {

        My_Log{} << "[TaskComplexityEvaluator] LocalModel input limit: "
                 << "context_size=" << context_size
                 << ", max_input_tokens=" << max_input_tokens
                 << std::endl;
        std::vector<std::string> segments;
        size_t total_tokens = 0;

        for (int i = static_cast<int>(messages.size()) - 1; i >= 0; --i) {
            const auto& msg = messages[i];
            std::string role = msg.value("role", "");
            if (role == "system") continue;

            std::string text = extract_msg_text(msg);
            if (text.empty()) continue;

            size_t remaining_tokens = max_input_tokens - total_tokens;
            if (remaining_tokens == 0) break;

            size_t text_tokens = handle->TokenLength(text);

            if (text_tokens > remaining_tokens) {

                size_t estimated_chars = text.size() * remaining_tokens / text_tokens;
                std::string truncated = safe_utf8_truncate(text, estimated_chars, "");

                for (int iter = 0; iter < 3; ++iter) {
                    size_t t = handle->TokenLength(truncated);
                    if (t <= remaining_tokens) break;
                    size_t remove = std::max(size_t(1), truncated.size() / 10);
                    truncated = safe_utf8_truncate(truncated, truncated.size() - remove, "");
                }
                if (!truncated.empty()) {
                    segments.push_back(truncated);
                    total_tokens += handle->TokenLength(truncated);
                }
                break;
            }

            segments.push_back(text);
            total_tokens += text_tokens;

            if (total_tokens >= max_input_tokens) break;
        }

        if (segments.empty()) return "";

        std::reverse(segments.begin(), segments.end());

        std::string full_text;
        for (const auto& s : segments) {
            full_text += s;
            full_text += ' ';
        }

        if (!full_text.empty() && full_text.back() == ' ') {
            full_text.pop_back();
        }
        return full_text;

    } else {

        size_t max_chars = static_cast<size_t>(config_.model_input_max_chars);
        std::string full_text;

        for (int i = static_cast<int>(messages.size()) - 1; i >= 0; --i) {
            const auto& msg = messages[i];
            if (!msg.contains("role") || !msg["role"].is_string()) continue;
            if (msg["role"].get<std::string>() != "user") continue;

            full_text = extract_msg_text(msg);
            break;
        }

        if (full_text.size() > max_chars) {
            full_text = safe_utf8_truncate(full_text, max_chars, "");
        }
        return full_text;
    }
}

std::string TaskComplexityEvaluator::ComputeHash(const std::string &text)
{
    uint64_t h = SecurityUtils::FNV1aHash(text);
    char buf[17];
    snprintf(buf, sizeof(buf), "%016llx", static_cast<unsigned long long>(h));
    return std::string(buf);
}

bool TaskComplexityEvaluator::CacheLookup(const std::string &key, CacheEntry &entry)
{
    std::lock_guard<std::mutex> lock(cache_mutex_);
    auto it = cache_map_.find(key);
    if (it == cache_map_.end())
        return false;

    cache_list_.splice(cache_list_.begin(), cache_list_, it->second);
    entry = it->second->second;
    return true;
}

void TaskComplexityEvaluator::CacheInsert(const std::string &key, const CacheEntry &entry)
{
    std::lock_guard<std::mutex> lock(cache_mutex_);

    auto it = cache_map_.find(key);
    if (it != cache_map_.end())
    {
        cache_list_.erase(it->second);
        cache_map_.erase(it);
    }

    cache_list_.push_front({key, entry});
    cache_map_[key] = cache_list_.begin();

    if (cache_list_.size() > MAX_CACHE_SIZE)
    {
        const std::string &lru_key = cache_list_.back().first;
        cache_map_.erase(lru_key);
        cache_list_.pop_back();
    }
}

bool TaskComplexityEvaluator::LocalModelEvaluate(
        const std::string &task_summary,
        int timeout_ms,
        ComplexityLevel &result_level,
        std::string &reason)
{
    if (!model_config_)
    {
        reason = "model_config not available";
        return false;
    }

    auto handle = model_config_->GetDefaultModelHandle().lock();
    if (!handle)
    {
        reason = "default model handle not available";
        return false;
    }

    const auto *default_instance_config = model_config_->GetDefaultInstanceConfig();
    const PromptType prompt_type = default_instance_config
            ? default_instance_config->get_prompt_type()
            : model_config_->get_prompt_type();
    const bool is_harmony_model = (prompt_type == PromptType::Harmony);
    const std::string complexity_json_prefill = "{\"complexity\":\"";

    std::string prompt = BuildLocalModelPrompt(model_config_, config_.system_prompt, "Text: " + task_summary,
                                               complexity_json_prefill);
    My_Log{} << "[TaskComplexityEvaluator] prompt format: "
             << (is_harmony_model ? "Harmony" : "General")
             << " for complexity check" << std::endl;

    auto query_result = SecurityUtils::LocalModelQuery(
            handle, prompt, complexity_json_prefill, timeout_ms, /*max_gen_tokens=*/0,
            [](std::string &output_buf, const std::string &token, bool &json_complete) -> bool {
                (void)token;

                size_t open_brace = output_buf.find('{');
                size_t close_brace = output_buf.rfind('}');
                if (open_brace != std::string::npos &&
                    close_brace != std::string::npos &&
                    close_brace > open_brace) {
                    std::string candidate = output_buf.substr(open_brace, close_brace - open_brace + 1);
                    bool is_valid_json = false;
                    try {
                        auto j = nlohmann::json::parse(candidate);
                        if (j.contains("complexity") && j["complexity"].is_string()) {
                            const std::string &cv = j["complexity"].get<std::string>();
                            is_valid_json = (cv == "C0" || cv == "C1" || cv == "C2");
                        }
                    } catch (...) {}
                    if (is_valid_json) {
                        json_complete = true;
                        My_Log{} << "[TaskComplexityEvaluator] [STREAM] JSON complete. Early stop. buf=\""
                                 << output_buf << "\"\n";
                        return false;
                    }
                }

                std::string completed_json, matched_value;
                if (SecurityUtils::CheckPrefixEarlyStop(output_buf, "{\"complexity\":\"", {"C0", "C1", "C2"}, completed_json, matched_value)) {
                    json_complete = true;
                    output_buf = completed_json;
                    My_Log{} << "[TaskComplexityEvaluator] [STREAM] Prefix match early stop: "
                             << "level=" << matched_value << ", completed_json=" << completed_json << "\n";
                    return false;
                }

                return true;
            },
            "[TaskComplexityEvaluator]");

    if (!query_result.success || query_result.output.empty())
    {
        reason = "local model query failed or timed out, conservative fallback";
        return false;
    }

    const std::string &model_output = query_result.output;
    try
    {

        size_t json_start = model_output.find('{');
        size_t json_end = model_output.rfind('}');
        if (json_start == std::string::npos || json_end == std::string::npos)
        {
            reason = "model output does not contain JSON: " + model_output.substr(0, 100);
            return false;
        }

        std::string json_str = model_output.substr(json_start, json_end - json_start + 1);
        json parsed = json::parse(json_str);

        if (!parsed.contains("complexity") || !parsed["complexity"].is_string())
        {
            reason = "model output missing 'complexity' field";
            return false;
        }

        std::string complexity_str = parsed["complexity"].get<std::string>();
        if (complexity_str == "C0")
            result_level = ComplexityLevel::C0;
        else if (complexity_str == "C1")
            result_level = ComplexityLevel::C1;
        else if (complexity_str == "C2")
            result_level = ComplexityLevel::C2;
        else
        {
            reason = "unknown complexity value: " + complexity_str;
            return false;
        }

        if (parsed.contains("reason") && parsed["reason"].is_string())
            reason = parsed["reason"].get<std::string>();
        else
            reason = "model classified as " + complexity_str;

        return true;
    }
    catch (const json::exception &e)
    {
        reason = "failed to parse model output JSON: " + std::string(e.what());
        return false;
    }
}

ComplexityResult TaskComplexityEvaluator::Evaluate(
        const json &messages,
        const json &tools,
        const InspectionContext &ctx)
{

    ComplexityResult result = HeuristicEvaluate(messages, tools);

    if (!ctx.is_internal_inspection &&
        config_.use_local_model_fallback &&
        result.complexity_level == ComplexityLevel::C0 &&
        model_config_ != nullptr)
    {

        std::string task_summary = ExtractTaskSummary(messages, tools);

        if (!task_summary.empty())
        {

            std::string cache_key = ComputeHash(task_summary);
            CacheEntry cached_entry;

            if (CacheLookup(cache_key, cached_entry))
            {

                My_Log{} << "[TaskComplexityEvaluator] LocalModel cache hit, level="
                         << to_string(cached_entry.level) << std::endl;

                if (static_cast<int>(cached_entry.level) > static_cast<int>(result.complexity_level))
                {
                    result.complexity_level = cached_entry.level;
                    result.reason = "local_model_cached: " + cached_entry.reason;
                }
            }
            else
            {

                ComplexityLevel model_level = ComplexityLevel::C0;
                std::string model_reason;

                bool model_ok = LocalModelEvaluate(
                    task_summary,
                    config_.timeout_ms,
                    model_level,
                    model_reason);

                if (model_ok)
                {

                    CacheInsert(cache_key, {model_level, model_reason});

                    My_Log{} << "[TaskComplexityEvaluator] LocalModel result: level="
                             << to_string(model_level) << ", reason=" << model_reason << std::endl;

                    if (static_cast<int>(model_level) > static_cast<int>(result.complexity_level))
                    {
                        result.complexity_level = model_level;
                        result.reason = "local_model: " + model_reason;
                    }
                }
                else
                {

                    ComplexityLevel elevated_level;
                    if (result.complexity_level == ComplexityLevel::C0)
                        elevated_level = ComplexityLevel::C1;
                    else if (result.complexity_level == ComplexityLevel::C1)
                        elevated_level = ComplexityLevel::C2;
                    else
                        elevated_level = ComplexityLevel::C2;

                    My_Log{My_Log::Level::kError}
                        << "[TaskComplexityEvaluator] LocalModel failed: " << model_reason
                        << ". Elevating complexity from " << to_string(result.complexity_level)
                        << " to " << to_string(elevated_level)
                        << " (conservative fallback per §七-A)" << std::endl;

                    result.complexity_level = elevated_level;
                    result.reason = "local_model_failed_elevated: " + model_reason
                        + " (elevated to " + to_string(elevated_level) + ")";
                }
            }
        }
    }
    else
    {

        std::string skip_reason;
        if (ctx.is_internal_inspection)
            skip_reason = "is_internal_inspection=true (prevent recursion)";
        else if (!config_.use_local_model_fallback)
            skip_reason = "use_local_model_fallback=false";
        else if (result.complexity_level != ComplexityLevel::C0)
            skip_reason = "heuristic result=" + to_string(result.complexity_level) + " (not C0, local model not needed)";
        else if (model_config_ == nullptr)
            skip_reason = "model_config=null";
        else
            skip_reason = "unknown";
        My_Log{} << "[TaskComplexityEvaluator] LocalModel skipped: " << skip_reason << std::endl;
    }

    My_Log{} << "[TaskComplexityEvaluator] complexity=" << to_string(result.complexity_level)
             << ", reason=" << result.reason << std::endl;

    return result;
}
