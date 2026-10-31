//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef PROMPT_H
#define PROMPT_H

#include "../context/context_base.h"
#include "../chat_history/chat_history.h"
#include "../processor/harmony.h"
#include <nlohmann/json.hpp>
#include "prompt_optimizer.h"
#include "prompt_stats_helper.h"
#include "message_pre_filter.h"
#include "long_text_summarizer.h"
#include "summary_cache.h"
#include "task_memo_builder.h"
#include "task_memo_doc_scan.h"
#include "prompt_ledger.h"
#include "tool_call_circuit_breaker_store.h"
#include "tool_call_repetition_store.h"
#include <chrono>
#include <deque>
#include <functional>
#include <iosfwd>
#include <mutex>
#include <regex>
#include <set>
#include <unordered_map>
#include <unordered_set>

using json = nlohmann::ordered_json;

inline bool ShouldForceAllowAfterConsecutiveBlocks(int consecutive_block_count, int max_consecutive_block)
{
    return max_consecutive_block > 0 && consecutive_block_count >= max_consecutive_block;
}

inline bool ComputeForcedUnblockFromStreak(int &block_streak, int max_consecutive_block)
{
    ++block_streak;
    if (ShouldForceAllowAfterConsecutiveBlocks(block_streak, max_consecutive_block))
    {
        block_streak = 0;
        return true;
    }
    return false;
}

inline std::string BuildCycleRepeatSignature(const std::vector<std::string> &keys, size_t window)
{
    if (window == 0 || keys.empty())
        return "";
    const size_t begin = keys.size() > window ? keys.size() - window : 0;
    std::unordered_set<std::string> seen(keys.begin(), keys.begin() + begin);
    std::set<std::string> repeated;
    for (size_t i = begin; i < keys.size(); ++i)
    {
        if (keys[i].empty())
            continue;
        if (!seen.insert(keys[i]).second)
            repeated.insert(keys[i]);
    }
    if (repeated.empty())
        return "";
    std::string joined;
    for (const auto &key : repeated)
    {
        joined += key;
        joined += '\x1e';
    }
    return "cycle:" + std::to_string(std::hash<std::string>{}(joined));
}

inline bool IsReserveEligibleThinkReason(const std::string &reason)
{
    return reason == "new_failure" || reason == "changed_failure" || reason == "error_repeated" ||
           reason == "near_repeat" || reason.rfind("repeat_count", 0) == 0;
}

inline bool TryConsumeFailureThinkReserve(int &reserve_used, int reserve_limit)
{
    if (reserve_limit <= 0 || reserve_used >= reserve_limit)
        return false;
    ++reserve_used;
    return true;
}

inline std::string ComposeTaskMemoNotes(bool memo_body_embedded, const std::string &standalone_redundant,
                                        const std::string &consecutive_failure)
{
    return (memo_body_embedded ? std::string() : standalone_redundant) + consecutive_failure;
}

inline std::string ExtractLatestAssistantReadRef(const json &messages)
{
    if (!messages.is_array())
        return "";
    for (auto it = messages.rbegin(); it != messages.rend(); ++it) {
        if (!it->is_object() || it->value("role", "") != "assistant")
            continue;
        return TaskMemoBuilder::ExtractReadToolPath(get_json_value<std::string>(*it, "content", ""));
    }
    return "";
}

class ModelInputBuilder
{
public:
    ModelInputBuilder(ChatHistory &chat_history, ModelInstanceConfig *instance_config, const json &request_data = nullptr)
        : chat_history_{chat_history},
          instance_config_{instance_config},
          context_{instance_config_->i_model_config_.get_genie_model_handle().lock()},
          optimizer_{instance_config_->i_model_config_, context_.get(), instance_config_->get_context_size()},
          pre_filter_{instance_config_->i_model_config_, context_.get()},
          task_memo_builder_{instance_config_->i_model_config_.GetPromptOptimizationConfig().task_memo,
                              *instance_config_,
                              &TaskMemoStore::GetInstance(),
                              [this](const std::string& prompt) -> std::string { return this->RunSummarizationInference(prompt); }}
    {
        request_data_ = request_data;
    }

    ModelInput &Build(json &data, bool &is_tool,
                      std::function<bool()> is_alive_fn = nullptr)
    {
        Reset();

        if (data.contains("messages") && data["messages"].is_array()) {
            for (auto& e : data["messages"]) {
                if (e.contains("content") && e["content"].is_string()) {
                    std::string content = e["content"].get<std::string>();
                    clean_control_characters_inplace(content);
                    e["content"] = content;
                }
            }
        }

        if (!data.contains("messages") || !data["messages"].is_array()) {
            throw ReportError{std::string{"messages field is missing or not an array in request"}};
        }

        for (auto &e: data["messages"])
        {
            if (e["role"] == "user")
            {
                model_input_.image_.clear();
                model_input_.audio_.clear();
                json &user_content = e["content"];
                if (user_content.is_string())
                {
                    model_input_.text_ = user_content.get<std::string>();
                }
                else if (user_content.is_array())
                {
                    ProcessArray(user_content);
                }
                else if (user_content.is_object())
                {
                    ProcessObject(user_content);
                }
                else
                {
                    throw ReportError{"user content is not a object or array"};
                }
            }
            else if (e["role"] == "system")
            {
                json &system_content = e["content"];
                if (system_content.is_string())
                {
                    model_input_.system_ = system_content.get<std::string>();
                }
                else if (system_content.is_array())
                {
                    for (auto sys_element: system_content)
                    {
                        if (strcmp(sys_element["type"].get_ref<const std::string &>().c_str(), "text") == 0)
                        {
                            model_input_.system_ = sys_element["text"].get_ref<const std::string &>();
                            break;
                        }
                    }
                }
                else if (system_content.is_object())
                {
                    model_input_.system_ = get_json_value(e, "content", BLANK_STRING);
                }
                else
                {
                    throw ReportError{"system content is not a object or array"};
                }
                model_input_.system_ = str_replace(model_input_.system_, "\\n", "\n");
            }
        }

        if (model_input_.system_.empty())
        {
            My_Log{} << "system prompt is empty, will use default\n";
            model_input_.system_ = "You are a helpful assistant.";
        }

        if (model_input_.text_.empty())
        {
            My_Log{} << "user prompt is empty, will use default\n";
            model_input_.text_ = "What is this img or audio describe?";
        }

        const bool has_media = !model_input_.image_.empty() || !model_input_.audio_.empty();
        const bool last_message_is_user = !data["messages"].empty()
                                          && get_json_value(data["messages"].back(), "role", BLANK_STRING) == "user";
        if (has_media && last_message_is_user && context_ && context_->SupportsMultimodalInput())
        {
            return model_input_;
        }
        if (has_media)
        {
            const std::string reason = last_message_is_user
                ? "active model does not support multimodal input"
                : "last message is not a user message";
            My_Log{My_Log::Level::kWarning}
                << "[Multimodal] Dropping image/audio input because " << reason
                << "; continuing with the text prompt" << std::endl;
            model_input_.image_.clear();
            model_input_.audio_.clear();
        }

        bool is_harmony = (instance_config_->get_prompt_type() == PromptType::Harmony);

        const auto& po_cfg = instance_config_->i_model_config_.GetPromptOptimizationConfig();
        int min_output = instance_config_->getminOutputNum();
        int reserved_output = std::max(min_output,
                                       (int)(instance_config_->get_context_size() * po_cfg.output_reserve_ratio));
        int contextSize = instance_config_->get_context_size() - reserved_output;

        My_Log{}.original(true) << "\n";
        if(instance_config_)
        My_Log{My_Log::Level::kInfo} << "[Context] Total: " << instance_config_->get_context_size()
                                      << " tokens, Reserved: " << reserved_output
                                      << " tokens, Available: " << contextSize << " tokens" << std::endl;

        {
            auto sum_cfg = po_cfg.long_text_summarization;
            if (context_ && context_->UsesCrossRequestPrefixCache())
            {
                sum_cfg.summarize_tool_responses = false;
            }
            if (sum_cfg.enabled
                    && (sum_cfg.summarize_user_messages || sum_cfg.summarize_tool_responses)
                    && data.contains("messages")
                    && data["messages"].is_array())
            {
                auto infer_fn = [this, &is_alive_fn](const std::string& prompt) -> std::string {

                    ContextBase::PrefillHeartbeatCallback heartbeat = nullptr;
                    if (is_alive_fn) {
                        heartbeat = [&is_alive_fn]() -> bool {
                            return is_alive_fn();
                        };
                    }
                    return this->RunSummarizationInference(prompt, heartbeat);
                };

                LongTextSummarizer summarizer(
                    sum_cfg,
                    *instance_config_,
                    &SummaryCache::GetInstance(),
                    infer_fn,
                    static_cast<size_t>(contextSize),
                    is_alive_fn
                );

                last_ledger_.summarized = summarizer.ProcessMessages(data["messages"]);
            }
        }

        if (data.contains("tools") && data["tools"].is_array() && !data["tools"].empty())
        {
            const auto &repair_cfg = instance_config_->i_model_config_.GetToolCallRepairConfig();
            if (repair_cfg.enabled)
            {
                const std::string session_key = TaskMemoBuilder::ComputeFirstMsgKey(data["messages"]);
                const std::string cb_key = ToolCallCircuitBreakerStore::MakeKey(session_key, instance_config_->get_model_name());
                if (ToolCallCircuitBreakerStore::GetInstance().ShouldDowngradeToolDeclaration(cb_key))
                {
                    My_Log{My_Log::Level::kWarning}
                        << "[ToolCallRepair][CircuitBreaker] session+model '" << cb_key
                        << "' tripped consecutive Layer3 threshold; downgrading this request by "
                           "skipping tool declaration injection (model will not be told it can call tools)."
                        << std::endl;
                    data["tools"] = json::array();

                    ToolCallCircuitBreakerStore::GetInstance().RecordSuccess(cb_key);
                }
            }
        }

        if (is_harmony) {
            model_input_.text_ = BuildHarmonyPrompt(data, is_tool, contextSize);
        } else {
            model_input_.text_ = BuildPrompt(data, is_tool, contextSize);
        }

        if (model_input_.text_.empty())
        {
            throw ReportError{"build prompt failed"};
        }
        return model_input_;
    }

    const PromptLedger &GetLedger() const { return last_ledger_; }

    SkillMappings GetRuntimeSkillMappings() const { return optimizer_.GetLastStats().runtime_skill_mappings; }

private:
    void ProcessArray(const json &user_content)
    {
        static auto check_key{
                [](const json &content, const char *key, bool is_object = false) -> bool
                {
                    bool invalid;
                    if (!content.contains(key))
                    {
                        goto error;
                    }

                    invalid = is_object ? !content[key].is_object() : !content[key].is_string();
                    if (invalid)
                    {
                        goto error;
                    }

                    return true;
                    error:
                    My_Log{} << "user content " << key << " key is invalid\n";
                    return false;
                }
        };

        for (const auto &element: user_content)
        {
            if (!check_key(element, "type"))
            {
                continue;
            }

            if (strcmp(element["type"].get_ref<const std::string &>().c_str(), "text") == 0)
            {
                if (!check_key(element, "text"))
                {
                    continue;
                }
                model_input_.text_ = element["text"].get<std::string>();
            }

            if (strcmp(element["type"].get_ref<const std::string &>().c_str(), "image_url") == 0)
            {
                if (!check_key(element, "image_url", true))
                {
                    continue;
                }

                auto &j_image_url = element["image_url"];
                if (!check_key(j_image_url, "url"))
                {
                    continue;
                }

                auto img = j_image_url["url"].get<std::string>();
                int pos;
                if ((pos = img.find(',')) != std::string::npos)
                {
                    ++pos;
                    model_input_.image_ = img.substr(pos, img.size() - pos);
                }
            }

            if (strcmp(element["type"].get_ref<const std::string &>().c_str(), "input_audio") == 0)
            {
                if (!check_key(element, "input_audio", true))
                {
                    continue;
                }

                auto &j_input_audio = element["input_audio"];
                if (!check_key(j_input_audio, "data"))
                {
                    continue;
                }

                model_input_.audio_ = j_input_audio["data"].get<std::string>();
            }
        }
    }

    void ProcessObject(const json &user_content)
    {
        static auto get_value{
                [](const json &content, const char *key) -> std::string
                {
                    if (!content.contains(key))
                    {
                        My_Log{} << "msg does not contain " << key << " key\n";
                        return "";
                    }

                    if (!content[key].is_string())
                    {
                        throw ReportError{std::string{key} + " key is invalid"};
                    }

                    return content[key].get_ref<const std::string &>();
                }
        };

        model_input_.text_ = get_value(user_content, "question");
        model_input_.image_ = get_value(user_content, "image");
        model_input_.audio_ = get_value(user_content, "audio");
    }

    void PrepareOptimizedSystemAndToolPrompt(const json &tools, int contextSize, bool &is_tool,
                                              std::string &systemDefaultPrompt, std::string &raw_tools_str)
    {

        {
            My_Log{My_Log::Level::kDebug} << "[Optimization] Enabled" << std::endl;

            AgentType agentType = DetectAgentTypeAndLog(systemDefaultPrompt, "Optimization");

            model_input_.agent_type_ = (agentType == AgentType::MAIN_AGENT) ? "main" : "sub";

            if (agentType == AgentType::SUBAGENT) {
                systemDefaultPrompt = optimizer_.OptimizeSubagentSystemPrompt(systemDefaultPrompt, request_data_);
            } else {
                systemDefaultPrompt = optimizer_.OptimizeSystemPrompt(systemDefaultPrompt, request_data_);
            }

            auto stats = optimizer_.GetLastStats();
            My_Log{My_Log::Level::kInfo} << "[Optimization] System prompt rebuilt: original="
                                         << stats.original_tokens << " tok, rebuilt="
                                         << stats.optimized_tokens << " tok, delta="
                                         << -stats.savings_percent << "%" << std::endl;
        }

        std::string userToolsPrompt = "";
        if (tools.is_array() && !tools.empty())
        {
            is_tool = true;
            raw_tools_str = tools.dump();
            userToolsPrompt = raw_tools_str;
        }

        if (is_tool && !userToolsPrompt.empty()) {

            std::string tool_tmpl = instance_config_->get_tool_prompt_template();

            size_t identity_tokens = context_->TokenLength(systemDefaultPrompt);
            size_t tools_budget = (static_cast<size_t>(std::max(contextSize, 0)) > identity_tokens)
                                 ? static_cast<size_t>(contextSize) - identity_tokens : 0;
            userToolsPrompt = optimizer_.OptimizeToolsPrompt(userToolsPrompt, tool_tmpl, tools_budget, request_data_);
            userToolsPrompt += "\n\n";
            systemDefaultPrompt += userToolsPrompt;
        }
    }

    OptimizedMessages PrepareFilteredMessages(json &msg, const std::string &systemDefaultPrompt, int contextSize)
    {

        msg = pre_filter_.PreFilterMessages(msg, contextSize, systemDefaultPrompt, /*is_harmony=*/false, &tool_call_id_to_name_);

        task_memo_builder_.RecordRepeatDetections(pre_filter_.GetRepeatDetections());

        std::vector<GenieChatMessage> all_messages;

        int last_user_msg_index = -1;
        for (size_t i = 0; i < msg.size(); ++i) {
            if (get_json_value(msg[i], "role", BLANK_STRING) == "user") {
                last_user_msg_index = static_cast<int>(i);
            }
        }

        auto compute_tool_call_signature = [](const json& element) {
            if (!element.contains("tool_calls") || !element["tool_calls"].is_array()
                || element["tool_calls"].empty() || !element["tool_calls"][0].contains("function"))
                return std::string();
            const auto& fn = element["tool_calls"][0]["function"];
            std::string args = "{}";
            try {
                if (fn.contains("arguments") && fn["arguments"].is_string())
                    args = json::parse(fn["arguments"].get<std::string>()).dump();
                else if (fn.contains("arguments") && fn["arguments"].is_object())
                    args = fn["arguments"].dump();
            } catch (...) {}
            return fn.value("name", "") + "|" + args;
        };
        std::unordered_map<std::string, int> last_pin_element;
        for (size_t i = 0; i < msg.size(); ++i) {
            if (get_json_value(msg[i], "role", BLANK_STRING) == "assistant") {
                const std::string signature = compute_tool_call_signature(msg[i]);
                if (!signature.empty())
                    last_pin_element[signature] = static_cast<int>(i);
            }
        }

        int element_index = -1;
        for (auto &element: msg)
        {
            ++element_index;
            auto role = get_json_value(element, "role", BLANK_STRING);
            std::string content;

            if (role == "user")
            {
                if (element_index == last_user_msg_index)
                {

                    all_messages.push_back({role, model_input_.text_});
                }
                else
                {

                    content = get_json_value(element, "content", BLANK_STRING);
                    if (!content.empty()) {
                        all_messages.push_back({role, content});
                    }
                }
            }
            else if (role == "assistant")
            {

                if (element.contains("tool_calls") && !element["tool_calls"].is_null() &&
                    element["tool_calls"].is_array())
                {
                    auto converted_calls = optimizer_.ConvertOpenAIToolCalls(element["tool_calls"]);
                    std::string pin;
                    const std::string signature = compute_tool_call_signature(element);
                    auto last_pin = last_pin_element.find(signature);
                    if (last_pin != last_pin_element.end() && last_pin->second == element_index)
                        pin = TaskMemoBuilder::LookupPinnedConclusion(
                            TaskMemoBuilder::ComputeFirstMsgKey(
                                (request_data_.is_object() && request_data_.contains("messages")) ? request_data_["messages"] : msg),
                            signature);
                    for (const auto& converted_content : converted_calls)
                    {
                        all_messages.push_back({role, pin + converted_content});
                        pin.clear();
                    }
                }
                else
                {
                    content = get_json_value(element, "content", BLANK_STRING);
                    if (!content.empty()) {
                        all_messages.push_back({role, content});
                    }
                }
            }
            else if (role == "tool")
            {
                std::string tool_response = get_json_value(element, "content", BLANK_STRING);
                content = optimizer_.OptimizeToolResponse(tool_response);
                all_messages.push_back({role, content});
            }

        }

        if (last_user_msg_index < 0) {
            all_messages.push_back({"user", model_input_.text_});
        }

        chat_history_.Clear();
        OptimizedMessages optimized = ApplyFitMessagesToContext(
            all_messages,
            systemDefaultPrompt,
            contextSize,
            "Optimization"
        );
        if (!optimized.messages.empty()) {
            std::string memo_body = BuildTaskMemoBody();
            if (!memo_body.empty()) {
                size_t memo_idx = 0;
                while (memo_idx + 1 < optimized.messages.size() && optimized.messages[memo_idx].role == "system")
                    ++memo_idx;
                optimized.messages[memo_idx].content = memo_body.substr(2) + "\n\n" + optimized.messages[memo_idx].content;
            }
        }
        for (const auto& opt_msg : optimized.messages) {
            chat_history_.AddMessage(opt_msg.role, opt_msg.content);
        }

        return optimized;
    }

    std::string BuildPrompt(json &data, bool &is_tool, int contextSize)
    {
        is_tool = false;

        if (!data.contains("messages") || !data["messages"].is_array()) {
            throw ReportError{std::string{"messages field is missing or not an array in BuildPrompt"}};
        }
        json msg = data["messages"];

        json tools = (data.contains("tools") && data["tools"].is_array())
                     ? data["tools"] : json::array();

        size_t ledger_messages_in = msg.size();
        std::string ledger_raw_content_concat;
        for (const auto& ledger_m : msg) {
            std::string ledger_c = get_json_value(ledger_m, "content", BLANK_STRING);
            if (!ledger_c.empty()) {
                ledger_raw_content_concat += ledger_c;
                ledger_raw_content_concat += "\n";
            }
        }

        std::string systemDefaultPrompt = "You are a helpful assistant.";

        My_Log{My_Log::Level::kDebug} << "[Context] Window size: " << instance_config_->get_context_size() << " tokens" << std::endl;

        systemDefaultPrompt = model_input_.system_;

        std::string ledger_system_snapshot = systemDefaultPrompt;

        struct BeforeOptStats {
            bool valid = false;
            json messages_snapshot;
            std::string system_prompt_snapshot;
            std::shared_ptr<ContextBase> handle;
            int context_size_val = 0;
            const json* tools_ptr = nullptr;
            json tools_snapshot;
            size_t full_context_size_val = 0;
        } before_opt_stats;
        if (instance_config_->getenablePromptDebug()) {

            before_opt_stats.valid = true;
            before_opt_stats.messages_snapshot = msg;
            before_opt_stats.system_prompt_snapshot = systemDefaultPrompt;
            before_opt_stats.handle = context_;
            before_opt_stats.context_size_val = contextSize;
            before_opt_stats.full_context_size_val = instance_config_->get_context_size();
            if (tools.is_array() && !tools.empty()) {
                before_opt_stats.tools_snapshot = tools;
                before_opt_stats.tools_ptr = &before_opt_stats.tools_snapshot;
            }
        }

        std::string raw_tools_str;
        PrepareOptimizedSystemAndToolPrompt(tools, contextSize, is_tool, systemDefaultPrompt, raw_tools_str);

        size_t ledger_tokens_in = context_->TokenLength(
            ledger_system_snapshot + "\n" + ledger_raw_content_concat + raw_tools_str);

        bool force_empty_think_prefill = false;
        bool force_open_think_prefill = false;
        std::string think_goal_reminder;
        model_input_.think_time_budget_s_ = 0;
        model_input_.think_force_min_floor_ = false;
        if (instance_config_->is_thinking_model())
        {
            bool failure_trigger = false;
            bool forced_unblock_trigger = false;
            if (instance_config_->getenableThinking())
            {
                systemDefaultPrompt += "/think";
            }
            else if (ShouldThinkThisTurn(&failure_trigger, &forced_unblock_trigger))
            {
                const auto &at_cfg = instance_config_->i_model_config_.GetAdaptiveThinkingConfig();
                systemDefaultPrompt += "/no_think";
                force_open_think_prefill = true;
                if (failure_trigger && at_cfg.goal_reminder_chars > 0)
                    think_goal_reminder = BuildGoalReminder(at_cfg.goal_reminder_chars);
                model_input_.think_time_budget_s_ = at_cfg.time_budget_s;
                model_input_.think_min_tokens_ = at_cfg.min_tokens;
                model_input_.think_max_tokens_ = at_cfg.max_tokens;
                model_input_.think_ctx_reserve_ = at_cfg.ctx_reserve_tokens;
                model_input_.think_force_min_floor_ = forced_unblock_trigger;
            }
            else
            {
                systemDefaultPrompt += "/no_think";
                force_empty_think_prefill = instance_config_->is_speculative_draft_configured();
            }
        }

#if defined(_WIN32) || defined(__WIN32__) || defined(WIN32)
        {
            const auto &shell_hint_cfg = instance_config_->i_model_config_.GetWindowsShellHintConfig();
            if (shell_hint_cfg.enabled && tools.is_array()
                && model_input_.system_.find("## Host Environment") == std::string::npos
                && systemDefaultPrompt.find("## Host Environment") == std::string::npos)
            {
                bool has_shell_tool = false;
                for (const auto &tool : tools)
                {
                    if (!tool.contains("function") || !tool["function"].is_object())
                        continue;
                    const auto &func = tool["function"];
                    std::string name;
                    if (func.contains("name") && func["name"].is_string())
                        name = func["name"].get<std::string>();
                    std::string desc;
                    if (func.contains("description") && func["description"].is_string())
                        desc = func["description"].get<std::string>();
                    for (const auto &kw : shell_hint_cfg.tool_keywords)
                    {
                        if (str_contains(name, kw) || str_contains(desc, kw))
                        {
                            has_shell_tool = true;
                            break;
                        }
                    }
                    if (has_shell_tool)
                        break;
                }
                if (has_shell_tool)
                    systemDefaultPrompt += "\n" + shell_hint_cfg.hint_text;
            }
        }
#endif

        OptimizedMessages optimized = PrepareFilteredMessages(msg, systemDefaultPrompt, contextSize);

        auto &j = instance_config_->get_prompt_template();

        if (!j.is_object()) {
            My_Log{My_Log::Level::kError}
                << "[BuildPrompt] prompt_template is invalid (type=" << j.type_name()
                << ") for model '" << instance_config_->get_model_name()
                << "'. This model's prompt.json may not have been loaded correctly. "
                << "Check [LoadModel] logs for 'Error loading prompt file' or 'file.good()=false'." << std::endl;
            throw ReportError{std::string{"prompt_template is not a valid JSON object for model: "}
                              + instance_config_->get_model_name()
                              + " (type=" + j.type_name() + "). "
                              + "Ensure the model directory contains a valid prompt.json file."};
        }

        const std::string empty_think_start = j["start"].get<std::string>() + "<think>\n\n</think>\n\n";
        std::string start_prompt_str = j["start"].get<std::string>();
        if (force_empty_think_prefill)
        {
            start_prompt_str = empty_think_start;
        }
        else if (force_open_think_prefill)
        {
            start_prompt_str += "<think>\n" + think_goal_reminder;
        }
        const bool stable_empty_think_history = force_empty_think_prefill
            || (force_open_think_prefill && instance_config_->is_speculative_draft_configured());
        std::string modelInputContent = chat_history_.GetUserMessage(
                                                          str_replace(j["system"], "string", systemDefaultPrompt),
                                                          start_prompt_str,
                                                          stable_empty_think_history ? empty_think_start : std::string(),
                                                          BuildTaskMemoNotes());

        {
            size_t final_tokens = context_->TokenLength(modelInputContent);

            FillLedger(ledger_tokens_in, final_tokens, ledger_messages_in, optimized);

            std::ostringstream log_stream;
            log_stream << "[Normal] Final prompt - Tokens: " << final_tokens;

            if (final_tokens <= static_cast<size_t>(contextSize)) {
                size_t available_tokens = contextSize - final_tokens;
                log_stream << ", Available: " << available_tokens;
            } else {
                int exceeded = final_tokens - contextSize;
                log_stream << ", Exceeded: " << exceeded;
            }

            log_stream << ", Length: " << modelInputContent.length() << " chars";
            My_Log{My_Log::Level::kInfo} << log_stream.str() << std::endl;

            if (instance_config_->getenablePromptDebug()) {

                My_Log{My_Log::Level::kInfo} << "\n========== 详细过程日志 ==========" << std::endl;
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Output message details (raw JSON, before prompt conversion, oldest to newest):" << std::endl;
                size_t total_message_tokens_dbg = 0;
                for (size_t dbg_i = 0; dbg_i < msg.size(); ++dbg_i) {
                    const auto& dbg_element = msg[dbg_i];
                    auto dbg_role = get_json_value(dbg_element, "role", BLANK_STRING);
                    std::string dbg_content = get_json_value(dbg_element, "content", BLANK_STRING);
                    size_t dbg_msg_tokens = context_->TokenLength(dbg_content);
                    total_message_tokens_dbg += dbg_msg_tokens;

                    std::string dbg_preview = dbg_content;
                    if (dbg_role == "system" && dbg_preview.length() > 300) {
                        dbg_preview = dbg_preview.substr(0, 300) + "...";
                    }
                    std::replace(dbg_preview.begin(), dbg_preview.end(), '\n', ' ');
                    std::replace(dbg_preview.begin(), dbg_preview.end(), '\r', ' ');

                    std::ostringstream dbg_log;
                    dbg_log << "  [" << (dbg_i + 1) << "/" << msg.size() << "] "
                            << "Role: " << dbg_role << ", "
                            << "Tokens: " << dbg_msg_tokens;

                    if (dbg_role == "assistant" && dbg_element.contains("tool_calls") &&
                        !dbg_element["tool_calls"].is_null() && dbg_element["tool_calls"].is_array()) {
                        const auto& tcs = dbg_element["tool_calls"];
                        if (!tcs.empty()) {
                            dbg_log << ", ToolCalls: [";
                            for (size_t j = 0; j < tcs.size(); j++) {
                                if (j > 0) dbg_log << ", ";
                                std::string fn = tcs[j].value("function", json::object()).value("name", "");
                                dbg_log << fn;
                                if (tcs[j].contains("function") && tcs[j]["function"].contains("arguments")) {
                                    std::string fa = tcs[j]["function"]["arguments"].is_string() ?
                                        tcs[j]["function"]["arguments"].get<std::string>() :
                                        tcs[j]["function"]["arguments"].dump();
                                    dbg_log << "(" << fa << ")";
                                }
                            }
                            dbg_log << "]";
                        }
                    }

                    dbg_log << ", Content: \"" << dbg_preview << "\"";
                    My_Log{My_Log::Level::kInfo} << dbg_log.str() << std::endl;
                }
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Total messages tokens: " << total_message_tokens_dbg << std::endl;

                if (before_opt_stats.valid) {
                    PromptStatsHelper::PrintBeforeOptimizationStats(
                        before_opt_stats.messages_snapshot,
                        before_opt_stats.system_prompt_snapshot,
                        before_opt_stats.handle,
                        before_opt_stats.context_size_val,
                        before_opt_stats.tools_ptr,
                        before_opt_stats.full_context_size_val);
                }

                size_t system_tokens = context_->TokenLength(systemDefaultPrompt);
                size_t tools_tokens = 0;
                if (!raw_tools_str.empty()) {
                    tools_tokens = context_->TokenLength(raw_tools_str);
                }
                PromptStatsHelper::PrintAfterPreFilterStats(msg, context_, system_tokens, contextSize, tools_tokens, &pre_filter_.GetStats(),
                                                            instance_config_->get_context_size());

                ComputeAndPrintFitStats(optimized, context_, system_tokens, tools_tokens, contextSize);

                PromptStatsHelper::PrintFinalStats(final_tokens, modelInputContent.length(), contextSize, is_tool,
                                                   instance_config_->get_context_size());
            }

        }

        return modelInputContent;
    }

    void PrepareOptimizedHarmonySystemMessages(const std::string &instructions, bool has_tools,
                                                const json &tools, const std::string &knowledge_cutoff, const std::string &current_date,
                                                const std::string &reasoning_level, const IModelConfig &model_config,
                                                std::string &system_msg, std::string &developer_msg)
    {
        My_Log{My_Log::Level::kDebug} << "[Harmony] System prompt optimization enabled" << std::endl;

        AgentType agentType = DetectAgentTypeAndLog(instructions, "Harmony");

        model_input_.agent_type_ = (agentType == AgentType::MAIN_AGENT) ? "main" : "sub";

        if (agentType == AgentType::SUBAGENT) {

            system_msg = "<|start|>system<|message|>";
            system_msg += optimizer_.OptimizeHarmonySystemMessage(
                knowledge_cutoff,
                current_date,
                reasoning_level,
                has_tools
            );
            system_msg += "<|end|>";

            if (has_tools || !instructions.empty()) {
                developer_msg = "<|start|>developer<|message|>";
                developer_msg += "# System Context\n\n";
                developer_msg += optimizer_.OptimizeSubagentSystemPrompt(instructions, request_data_);
                if (has_tools) {

                    const SystemContextConfig& ctx_cfg = model_config.GetSystemContextConfig();
                    for (const auto& sec : ctx_cfg.sections) {
                        if (!sec.enabled) continue;
                        if (sec.title.find("Tool Usage Guidelines") != std::string::npos) {
                            developer_msg += "\n\n" + sec.title + "\n\n";
                            for (const auto& line : sec.lines) {
                                developer_msg += line + "\n";
                            }
                            break;
                        }
                    }
                    developer_msg += "\n# Tools\n\n## functions\n\n";
                    developer_msg += "namespace functions {\n\n";
                    developer_msg += optimizer_.ConvertToolsToOptimizedTypeScript(tools, request_data_);
                    developer_msg += "\n} // namespace functions";
                }
                developer_msg += "<|end|>";
            }
        } else {

            system_msg = "<|start|>system<|message|>";
            system_msg += optimizer_.OptimizeHarmonySystemMessage(
                knowledge_cutoff,
                current_date,
                reasoning_level,
                has_tools
            );
            system_msg += "<|end|>";

            if (has_tools || !instructions.empty()) {
                developer_msg = "<|start|>developer<|message|>";
                developer_msg += optimizer_.OptimizeHarmonyDeveloperMessage(
                    instructions,
                    tools,
                    request_data_
                );
                developer_msg += "<|end|>";
            }
        }

        My_Log{My_Log::Level::kDebug} << "[Harmony] Optimization completed" << std::endl;
    }

    OptimizedMessages PrepareHarmonyMessages(std::vector<GenieChatMessage> &processed_messages,
                                              const std::string &system_msg, const std::string &developer_msg,
                                              int contextSize, std::vector<std::string> &messages)
    {

        std::string full_system_prompt = system_msg + developer_msg;

        OptimizedMessages optimized;
        chat_history_.Clear();

        std::unordered_map<size_t, std::string> user_original_content;
        for (size_t pm_idx = 0; pm_idx < processed_messages.size(); pm_idx++) {
            if (processed_messages[pm_idx].role == "user") {
                user_original_content[pm_idx] = processed_messages[pm_idx].content;
                processed_messages[pm_idx].content = HarmonyProcessor::BuildUserMessage(processed_messages[pm_idx].content);
            }
        }

        optimized = ApplyFitMessagesToContext(
            processed_messages,
            full_system_prompt,
            contextSize,
            "Harmony"
        );

        std::vector<std::string> user_original_ordered;
        {
            std::vector<size_t> sorted_indices;
            for (const auto& kv : user_original_content) {
                sorted_indices.push_back(kv.first);
            }
            std::sort(sorted_indices.begin(), sorted_indices.end());
            for (size_t idx : sorted_indices) {
                user_original_ordered.push_back(user_original_content[idx]);
            }
        }
        size_t total_user_in_optimized = 0;
        for (const auto& m : optimized.messages) {
            if (m.role == "user") total_user_in_optimized++;
        }
        size_t dropped_user_count = (user_original_ordered.size() >= total_user_in_optimized)
                                    ? (user_original_ordered.size() - total_user_in_optimized)
                                    : 0;
        size_t user_original_cursor = dropped_user_count;
        for (const auto& opt_msg : optimized.messages) {
            if (opt_msg.role == "user") {
                if (user_original_cursor < user_original_ordered.size()) {
                    chat_history_.AddMessage(opt_msg.role, user_original_ordered[user_original_cursor]);
                    user_original_cursor++;
                } else {
                    My_Log{My_Log::Level::kWarning}
                        << "[Harmony] user_original_ordered exhausted, storing opt_msg.content directly" << std::endl;
                    chat_history_.AddMessage(opt_msg.role, opt_msg.content);
                }
            } else {
                chat_history_.AddMessage(opt_msg.role, opt_msg.content);
            }
        }

        for (const auto &msg_info : optimized.messages)
        {
            messages.push_back(msg_info.content);
        }

        if (!optimized.messages.empty()) {
            std::string memo_section = BuildTaskMemoSection();
            if (!memo_section.empty()) {
                messages.push_back(HarmonyProcessor::BuildUserMessage(memo_section));
            }
        }

        return optimized;
    }

    std::string BuildHarmonyPrompt(json &data, bool &is_tool, int contextSize)
    {
        const IModelConfig & model_config{instance_config_->i_model_config_};
        is_tool = false;

        if (!data.contains("messages") || !data["messages"].is_array()) {
            throw ReportError{std::string{"messages field is missing or not an array in BuildHarmonyPrompt"}};
        }
        json msg = data["messages"];

        json tools = (data.contains("tools") && data["tools"].is_array())
                     ? data["tools"] : json::array();

        size_t ledger_messages_in = msg.size();
        std::string ledger_raw_content_concat;
        for (const auto& ledger_m : msg) {
            std::string ledger_c = get_json_value(ledger_m, "content", BLANK_STRING);
            if (!ledger_c.empty()) {
                ledger_raw_content_concat += ledger_c;
                ledger_raw_content_concat += "\n";
            }
        }

        bool has_tools = tools.is_array() && !tools.empty();
        if (has_tools) {
            is_tool = true;
        }

        std::string current_date = getCurrentDate();

        auto &j = instance_config_->get_prompt_template();
        std::string reasoning_level = j.value("reasoning_level", "medium");
        std::string knowledge_cutoff = j.value("knowledge_cutoff", "2024-06");

        std::string instructions = "You are a helpful assistant.";
        for (const auto &element: msg)
        {
            auto role = get_json_value(element, "role", BLANK_STRING);
            if (role == "system")
            {
                instructions = get_json_value(element, "content", BLANK_STRING);
                instructions = str_replace(instructions, "\\n", "\n");
                break;
            }
        }

        std::string ledger_raw_tools_str = has_tools ? tools.dump() : "";
        size_t ledger_tokens_in = context_->TokenLength(
            instructions + "\n" + ledger_raw_content_concat + ledger_raw_tools_str);

        std::string system_msg;
        std::string developer_msg;

        PrepareOptimizedHarmonySystemMessages(instructions, has_tools, tools,
                                               knowledge_cutoff, current_date, reasoning_level, model_config,
                                               system_msg, developer_msg);

        for (const auto &pre_element : msg)
        {
            auto pre_role = get_json_value(pre_element, "role", BLANK_STRING);
            if (pre_role == "assistant" &&
                pre_element.contains("tool_calls") &&
                !pre_element["tool_calls"].is_null() &&
                pre_element["tool_calls"].is_array())
            {
                for (const auto &tc : pre_element["tool_calls"])
                {
                    std::string call_id;
                    if (tc.contains("id") && tc["id"].is_string())
                        call_id = tc["id"].get<std::string>();

                    std::string func_name;
                    if (tc.contains("function") && tc["function"].is_object())
                    {
                        const auto &func = tc["function"];
                        if (func.contains("name") && func["name"].is_string())
                            func_name = func["name"].get<std::string>();
                    }

                    if (!call_id.empty() && !func_name.empty())
                    {
                        tool_call_id_to_name_[call_id] = func_name;
                        My_Log{My_Log::Level::kDebug}
                            << "[PreScan] Saved tool_call_id mapping: "
                            << call_id << " -> " << func_name << std::endl;
                    }
                }
            }
        }
        My_Log{My_Log::Level::kDebug}
            << "[PreScan] Total tool_call_id mappings: "
            << tool_call_id_to_name_.size() << std::endl;

        json before_opt_msg_snapshot = msg;

        msg = pre_filter_.PreFilterMessages(msg, contextSize, system_msg + developer_msg, /*is_harmony=*/true, &tool_call_id_to_name_);

        task_memo_builder_.RecordRepeatDetections(pre_filter_.GetRepeatDetections());

        std::vector<std::string> messages;
        messages.push_back(system_msg);
        if (!developer_msg.empty()) {
            messages.push_back(developer_msg);
        }

        std::vector<GenieChatMessage> all_messages;

        all_messages.push_back({"user", model_input_.text_});
        for (const auto &element: msg)
        {
            auto role = get_json_value(element, "role", BLANK_STRING);
            std::string content;

            if (role == "assistant")
            {

                if (element.contains("tool_calls") && !element["tool_calls"].is_null() &&
                    element["tool_calls"].is_array())
                {
                    const auto& tool_calls = element["tool_calls"];
                    size_t num_tool_calls = tool_calls.size();

                    if (num_tool_calls > 1) {
                        My_Log{My_Log::Level::kDebug} << "[Tool Call] Multiple tool calls detected: " << num_tool_calls << std::endl;

                        std::string preamble = "**Action plan**:\n";
                        for (size_t i = 0; i < num_tool_calls; i++) {
                            std::string func_name = tool_calls[i].value("function", json::object()).value("name", "");
                            if (!func_name.empty()) {
                                preamble += std::to_string(i + 1) + ". Call function `" + func_name + "`\n";
                            }
                        }
                        preamble += "---\nWill start executing the plan step by step";

                        std::string preamble_msg = "<|start|>assistant<|channel|>commentary<|message|>";
                        preamble_msg += preamble + "<|end|>";
                        all_messages.push_back({role, preamble_msg});

                        My_Log{My_Log::Level::kDebug} << "[Tool Call] Added preamble for multiple tool calls" << std::endl;
                    }

                    for (const auto& tool_call : tool_calls)
                    {
                        std::string call_id = tool_call.value("id", "");
                        std::string func_name = tool_call.value("function", json::object()).value("name", "");
                        std::string func_args = tool_call.value("function", json::object()).value("arguments", "");

                        if (func_name.empty()) {
                            My_Log{My_Log::Level::kError}
                                << "[Tool Call] ERROR: Tool call with empty function name detected!" << std::endl;
                            My_Log{My_Log::Level::kError}
                                << "[Tool Call] Tool call ID: " << (call_id.empty() ? "(empty)" : call_id) << std::endl;
                            My_Log{My_Log::Level::kError}
                                << "[Tool Call] This indicates a malformed tool_calls array from the client." << std::endl;
                            continue;
                        }

                        try {
                            if (!func_args.empty()) {
                                json args_json = json::parse(func_args);
                                My_Log{My_Log::Level::kDebug} << "[Tool Call] ✓ JSON validation passed for: " << func_name << std::endl;
                            } else {

                                func_args = "{}";
                                My_Log{My_Log::Level::kDebug} << "[Tool Call] ✓ Empty arguments, using {} for: " << func_name << std::endl;
                            }
                        } catch (const std::exception& e) {
                            My_Log{My_Log::Level::kError}
                                << "[Tool Call] ✗ Invalid JSON arguments for " << func_name << std::endl;
                            My_Log{My_Log::Level::kError}
                                << "[Tool Call] Error: " << e.what() << std::endl;
                            My_Log{My_Log::Level::kError}
                                << "[Tool Call] Raw arguments: " << func_args << std::endl;

                            try {
                                json fixed_args;
                                fixed_args["raw_input"] = func_args;
                                func_args = fixed_args.dump();
                                My_Log{My_Log::Level::kDebug}
                                    << "[Tool Call] ⚠ Fixed invalid JSON by wrapping as raw_input" << std::endl;
                            } catch (...) {
                                My_Log{My_Log::Level::kError}
                                    << "[Tool Call] ✗ Failed to fix JSON, skipping tool call: " << func_name << std::endl;
                                continue;
                            }
                        }

                        if (call_id.empty()) {
                            My_Log{My_Log::Level::kDebug}
                                << "[Tool Call] ⚠ Tool call without ID for: " << func_name << std::endl;
                        }

                        std::string analysis_msg = "<|start|>assistant<|channel|>analysis<|message|>";
                        analysis_msg += "Need to use function " + func_name + ".<|end|>";
                        all_messages.push_back({role, analysis_msg});

                        My_Log{My_Log::Level::kDebug} << "[Tool Call] ✓ Added analysis message for: " << func_name << std::endl;

                        std::string commentary_msg = "<|start|>assistant<|channel|>commentary to=functions.";
                        commentary_msg += func_name;
                        commentary_msg += " <|constrain|>json";
                        commentary_msg += "<|message|>";
                        commentary_msg += func_args;
                        commentary_msg += "<|call|>";
                        all_messages.push_back({role, commentary_msg});

                        My_Log{My_Log::Level::kDebug} << "[Tool Call] ✓ Added commentary message for: " << func_name << std::endl;
                    }
                }
                else
                {
                    content = get_json_value(element, "content", BLANK_STRING);
                    if (!content.empty())
                    {

                        bool is_valid_harmony = false;

                        if (content.find("<|start|>assistant") != std::string::npos &&
                            content.find("<|channel|>") != std::string::npos &&
                            content.find("<|message|>") != std::string::npos &&
                            (content.find("<|end|>") != std::string::npos ||
                             content.find("<|call|>") != std::string::npos))
                        {
                            is_valid_harmony = true;
                            My_Log{} << "[Format] Valid Harmony format detected" << std::endl;
                        }

                        if (is_valid_harmony)
                        {
                            all_messages.push_back({role, content});
                            My_Log{} << "[Format] Using existing Harmony format" << std::endl;
                        }
                        else
                        {

                            std::string harmony_msg = HarmonyProcessor::BuildAssistantMessage(
                                "final", content
                            );
                            all_messages.push_back({role, harmony_msg});
                            My_Log{My_Log::Level::kDebug} << "[Format] Converted to Harmony format (final channel)" << std::endl;
                        }
                    }
                }
            }
            else if (role == "tool")
            {
                content = get_json_value(element, "content", BLANK_STRING);
                if (!content.empty())
                {
                    std::string tool_name;

                    if (element.contains("name") && element["name"].is_string())
                    {
                        tool_name = element["name"].get<std::string>();
                        My_Log{My_Log::Level::kDebug} << "[Tool Response] Using tool name from 'name' field: " << tool_name << std::endl;
                    }

                    else if (element.contains("tool_call_id") && element["tool_call_id"].is_string())
                    {
                        std::string call_id = element["tool_call_id"].get<std::string>();
                        auto it = tool_call_id_to_name_.find(call_id);
                        if (it != tool_call_id_to_name_.end())
                        {
                            tool_name = it->second;
                            My_Log{My_Log::Level::kDebug} << "[Tool Response] Found tool name from mapping: " << tool_name
                                     << " (call_id: " << call_id << ")" << std::endl;
                        }
                        else
                        {
                            My_Log{My_Log::Level::kDebug}
                                << "[Tool Response] tool_call_id not found in mapping: " << call_id << std::endl;
                        }
                    }

                    if (tool_name.empty())
                    {
                        tool_name = "unknown_tool";

                        My_Log{My_Log::Level::kWarning}
                            << "[Tool Response] Cannot determine tool name, using default: " << tool_name << std::endl;
                        My_Log{My_Log::Level::kWarning}
                            << "[Tool Response] This may indicate missing 'name' field in tool response" << std::endl;

                        if (element.contains("tool_call_id")) {
                            My_Log{My_Log::Level::kWarning}
                                << "[Tool Response] Provided tool_call_id: " << element["tool_call_id"] << std::endl;
                        } else {
                            My_Log{My_Log::Level::kWarning}
                                << "[Tool Response] No tool_call_id provided" << std::endl;
                        }

                        if (element.contains("name")) {
                            My_Log{My_Log::Level::kWarning}
                                << "[Tool Response] Provided name field (invalid): " << element["name"] << std::endl;
                        } else {
                            My_Log{My_Log::Level::kWarning}
                                << "[Tool Response] No name field provided" << std::endl;
                        }

                        if (!tool_call_id_to_name_.empty()) {
                            My_Log{My_Log::Level::kWarning}
                                << "[Tool Response] Available tool_call_id mappings: " << tool_call_id_to_name_.size() << std::endl;
                        } else {
                            My_Log{My_Log::Level::kWarning}
                                << "[Tool Response] No tool_call_id mappings available (may be due to message filtering)" << std::endl;
                        }
                    }

                    if (content.find("<|start|>functions.") != std::string::npos)
                    {
                        all_messages.push_back({role, content});
                        My_Log{My_Log::Level::kDebug} << "[Tool Response] Already in Harmony format" << std::endl;
                    }
                    else
                    {

                        std::string content_for_harmony = optimizer_.InjectSpawnGuard(content);

                        std::string harmony_msg = HarmonyProcessor::BuildToolMessage(
                            tool_name,
                            content_for_harmony
                        );
                        all_messages.push_back({role, harmony_msg});
                        My_Log{My_Log::Level::kDebug} << "[Tool Response] ✓ Converted to Harmony format: functions." << tool_name << std::endl;
                    }
                }
            }

        }

        std::vector<GenieChatMessage> processed_messages;

        struct ToolCallRange {
            size_t start;
            size_t end;
        };

        std::vector<ToolCallRange> tool_call_ranges;

        enum class CoTState { IDLE, PENDING_CALL, IN_TOOL_CALL, IN_TOOL_RESP };
        CoTState cot_state = CoTState::IDLE;
        size_t range_start = 0;
        size_t range_end   = 0;

        auto is_analysis_msg = [](const std::string& content) -> bool {
            return content.find("<|channel|>analysis") != std::string::npos;
        };

        auto is_preamble_msg = [](const std::string& content) -> bool {
            return content.find("<|channel|>commentary") != std::string::npos &&
                   content.find("to=functions.") == std::string::npos;
        };

        for (size_t i = 0; i < all_messages.size(); i++) {
            const auto& cur_msg = all_messages[i];

            if (cur_msg.role == "assistant") {
                bool is_tool_call = IsToolCallMessage(cur_msg.content);
                bool is_analysis  = is_analysis_msg(cur_msg.content);
                bool is_preamble  = is_preamble_msg(cur_msg.content);

                switch (cot_state) {
                case CoTState::IDLE:
                    if (is_analysis || is_preamble) {
                        range_start = i;
                        cot_state = CoTState::PENDING_CALL;
                        My_Log{My_Log::Level::kDebug} << "[CoT SM] IDLE→PENDING_CALL at [" << i
                            << "] (" << (is_analysis ? "analysis" : "preamble") << ")" << std::endl;
                    } else if (is_tool_call) {
                        range_start = i;
                        range_end   = i;
                        cot_state = CoTState::IN_TOOL_CALL;
                        My_Log{My_Log::Level::kDebug} << "[CoT SM] IDLE→IN_TOOL_CALL at [" << i << "]" << std::endl;
                    }
                    break;

                case CoTState::PENDING_CALL:
                    if (is_analysis || is_preamble) {
                        My_Log{My_Log::Level::kDebug} << "[CoT SM] PENDING_CALL: additional pre-call msg at [" << i
                            << "] (" << (is_analysis ? "analysis" : "preamble") << ")" << std::endl;
                    } else if (is_tool_call) {
                        range_end = i;
                        cot_state = CoTState::IN_TOOL_CALL;
                        My_Log{My_Log::Level::kDebug} << "[CoT SM] PENDING_CALL→IN_TOOL_CALL at [" << i
                            << "] (start=" << range_start << ")" << std::endl;
                    } else {
                        My_Log{My_Log::Level::kDebug} << "[CoT SM] PENDING_CALL→IDLE at [" << i
                            << "] (pre-call msgs not followed by tool_call, discarding candidate start="
                            << range_start << ")" << std::endl;
                        cot_state = CoTState::IDLE;
                    }
                    break;

                case CoTState::IN_TOOL_CALL:
                    My_Log{My_Log::Level::kDebug} << "[CoT SM] IN_TOOL_CALL: unexpected assistant at [" << i
                        << "], committing range [" << range_start << ", " << range_end << "]" << std::endl;
                    tool_call_ranges.push_back({range_start, range_end});
                    cot_state = CoTState::IDLE;
                    if (is_analysis || is_preamble) {
                        range_start = i;
                        cot_state = CoTState::PENDING_CALL;
                    } else if (is_tool_call) {
                        range_start = i;
                        range_end   = i;
                        cot_state = CoTState::IN_TOOL_CALL;
                    }
                    break;

                case CoTState::IN_TOOL_RESP:
                    if (is_analysis || is_preamble) {
                        My_Log{My_Log::Level::kDebug} << "[CoT SM] IN_TOOL_RESP→PENDING_CALL at [" << i
                            << "], committing range [" << range_start << ", " << range_end << "]" << std::endl;
                        tool_call_ranges.push_back({range_start, range_end});
                        range_start = i;
                        cot_state = CoTState::PENDING_CALL;
                    } else if (is_tool_call) {
                        My_Log{My_Log::Level::kDebug} << "[CoT SM] IN_TOOL_RESP→IN_TOOL_CALL at [" << i
                            << "], committing range [" << range_start << ", " << range_end << "]" << std::endl;
                        tool_call_ranges.push_back({range_start, range_end});
                        range_start = i;
                        range_end   = i;
                        cot_state = CoTState::IN_TOOL_CALL;
                    } else {
                        range_end = i;
                        My_Log{My_Log::Level::kDebug} << "[CoT SM] IN_TOOL_RESP→IDLE at [" << i
                            << "], committing range [" << range_start << ", " << range_end << "]" << std::endl;
                        tool_call_ranges.push_back({range_start, range_end});
                        cot_state = CoTState::IDLE;
                    }
                    break;
                }

            } else if (cur_msg.role == "tool") {
                switch (cot_state) {
                case CoTState::IN_TOOL_CALL:
                    range_end = i;
                    cot_state = CoTState::IN_TOOL_RESP;
                    My_Log{My_Log::Level::kDebug} << "[CoT SM] IN_TOOL_CALL→IN_TOOL_RESP at [" << i << "]" << std::endl;
                    break;
                case CoTState::IN_TOOL_RESP:
                    range_end = i;
                    My_Log{My_Log::Level::kDebug} << "[CoT SM] IN_TOOL_RESP: additional tool response at [" << i << "]" << std::endl;
                    break;
                case CoTState::PENDING_CALL:
                    My_Log{My_Log::Level::kDebug} << "[CoT SM] PENDING_CALL: unexpected tool at [" << i
                        << "], discarding candidate start=" << range_start << std::endl;
                    cot_state = CoTState::IDLE;
                    break;
                default:
                    break;
                }

            } else if (cur_msg.role == "user") {
                if (cot_state == CoTState::IN_TOOL_CALL || cot_state == CoTState::IN_TOOL_RESP) {
                    My_Log{My_Log::Level::kDebug} << "[CoT SM] →IDLE at [" << i
                        << "] (user msg), committing range [" << range_start << ", " << range_end << "]" << std::endl;
                    tool_call_ranges.push_back({range_start, range_end});
                } else if (cot_state == CoTState::PENDING_CALL) {
                    My_Log{My_Log::Level::kDebug} << "[CoT SM] PENDING_CALL→IDLE at [" << i
                        << "] (user msg), discarding candidate start=" << range_start << std::endl;
                }
                cot_state = CoTState::IDLE;
            }
        }
        if (cot_state == CoTState::IN_TOOL_CALL || cot_state == CoTState::IN_TOOL_RESP) {
            My_Log{My_Log::Level::kDebug} << "[CoT SM] End of messages, committing final range ["
                << range_start << ", " << range_end << "]" << std::endl;
            tool_call_ranges.push_back({range_start, range_end});
        }

        if (instance_config_->getenablePromptDebug()) {
            My_Log{My_Log::Level::kDebug} << "[CoT SM] Total tool call ranges: "
                << tool_call_ranges.size() << std::endl;
            for (size_t ri = 0; ri < tool_call_ranges.size(); ri++) {
                My_Log{My_Log::Level::kDebug} << "[CoT SM]   Range[" << ri << "]: ["
                    << tool_call_ranges[ri].start << ", " << tool_call_ranges[ri].end << "]" << std::endl;
            }
        }

        std::vector<bool> msg_in_range(all_messages.size(), false);
        std::vector<bool> msg_is_endpoint(all_messages.size(), false);
        for (const auto& range : tool_call_ranges) {
            for (size_t k = range.start; k <= range.end && k < all_messages.size(); k++) {
                msg_in_range[k] = true;
                if (k == range.end) msg_is_endpoint[k] = true;
            }
        }

        for (size_t i = 0; i < all_messages.size(); i++) {
            const auto &msg_info = all_messages[i];

            bool in_tool_call_range = msg_in_range[i];
            bool is_range_endpoint  = msg_is_endpoint[i];

            if (msg_info.role == "tool") {

                processed_messages.push_back(msg_info);
                My_Log{My_Log::Level::kDebug} << "[CoT] Tool response at index " << i << ": keeping full message" << std::endl;
            }
            else if (msg_info.role == "assistant") {
                if (in_tool_call_range) {

                    bool is_tool_related = IsToolCallMessage(msg_info.content) ||
                                          msg_info.content.find("<|channel|>analysis") != std::string::npos ||
                                          (msg_info.content.find("<|channel|>commentary") != std::string::npos &&
                                           msg_info.content.find("to=functions.") == std::string::npos);

                    if (is_tool_related) {
                        processed_messages.push_back(msg_info);
                        My_Log{My_Log::Level::kDebug} << "[CoT] Tool-related assistant at index " << i
                                << ": keeping full message (analysis/commentary/preamble)" << std::endl;
                    } else if (is_range_endpoint) {
                        std::string final_only = ExtractFinalChannel(msg_info.content);
                        if (!final_only.empty()) {
                            processed_messages.push_back({msg_info.role, final_only});
                            My_Log{My_Log::Level::kDebug} << "[CoT] Tool-result assistant at range endpoint " << i
                                    << ": keeping only final channel" << std::endl;
                        } else {

                            processed_messages.push_back(msg_info);
                            My_Log{My_Log::Level::kDebug} << "[CoT] Tool-result assistant at range endpoint " << i
                                    << ": no final channel found, keeping original" << std::endl;
                        }
                    } else {

                        processed_messages.push_back(msg_info);
                        My_Log{My_Log::Level::kDebug} << "[CoT] In-range assistant at index " << i
                                << ": keeping full message (fallback)" << std::endl;
                    }
                } else {

                    std::string final_only = ExtractFinalChannel(msg_info.content);
                    if (!final_only.empty()) {
                        processed_messages.push_back({msg_info.role, final_only});
                        My_Log{My_Log::Level::kDebug} << "[CoT] Regular assistant at index " << i
                                << ": keeping only final channel" << std::endl;
                    } else {

                        processed_messages.push_back(msg_info);
                        My_Log{My_Log::Level::kDebug}
                            << "[CoT] Regular assistant at index " << i
                            << ": no final channel found, keeping original" << std::endl;
                    }
                }
            }
            else {

                processed_messages.push_back(msg_info);
            }
        }

        OptimizedMessages optimized = PrepareHarmonyMessages(processed_messages,
                                                              system_msg, developer_msg, contextSize, messages);

        messages.push_back("<|start|>assistant");

        std::string result;
        for (const auto &harmony_part : messages) {
            result += harmony_part;
        }

        size_t ledger_final_tokens = context_->TokenLength(result);
        if (instance_config_->getenablePromptDebug()) {

            My_Log{My_Log::Level::kInfo} << "\n========== 详细过程日志 ==========" << std::endl;
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Output message details (raw JSON, before Harmony conversion, oldest to newest):" << std::endl;
            size_t total_message_tokens = 0;
            for (size_t i = 0; i < msg.size(); ++i) {
                const auto& element = msg[i];
                auto role = get_json_value(element, "role", BLANK_STRING);
                std::string content = get_json_value(element, "content", BLANK_STRING);
                size_t msg_tokens = context_->TokenLength(content);
                total_message_tokens += msg_tokens;

                std::string content_preview = content;
                if (role == "system" && content_preview.length() > 300) {
                    content_preview = content_preview.substr(0, 300) + "...";
                }
                std::replace(content_preview.begin(), content_preview.end(), '\n', ' ');
                std::replace(content_preview.begin(), content_preview.end(), '\r', ' ');

                std::ostringstream log_stream;
                log_stream << "  [" << (i + 1) << "/" << msg.size() << "] "
                           << "Role: " << role << ", "
                           << "Tokens: " << msg_tokens;

                if (role == "assistant" && element.contains("tool_calls") &&
                    !element["tool_calls"].is_null() && element["tool_calls"].is_array()) {
                    const auto& tool_calls = element["tool_calls"];
                    if (!tool_calls.empty()) {
                        log_stream << ", ToolCalls: [";
                        for (size_t j = 0; j < tool_calls.size(); j++) {
                            if (j > 0) log_stream << ", ";
                            const auto& tool_call = tool_calls[j];
                            std::string func_name = tool_call.value("function", json::object()).value("name", "");
                            log_stream << func_name;

                            if (tool_call.contains("function") && tool_call["function"].contains("arguments")) {
                                std::string func_args = tool_call["function"]["arguments"].is_string() ?
                                    tool_call["function"]["arguments"].get<std::string>() :
                                    tool_call["function"]["arguments"].dump();
                                log_stream << "(" << func_args << ")";
                            }
                        }
                        log_stream << "]";
                    }
                }

                log_stream << ", Content: \"" << content_preview << "\"";

                My_Log{My_Log::Level::kInfo} << log_stream.str() << std::endl;
            }
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Total messages tokens: " << total_message_tokens << std::endl;

            if (context_) {
                PromptStatsHelper::PrintBeforeOptimizationStatsForHarmony(
                    before_opt_msg_snapshot, optimizer_, context_, contextSize,
                    knowledge_cutoff, current_date, reasoning_level, has_tools,
                    instructions, tools, instance_config_->get_context_size());
            }

            size_t system_tokens = context_->TokenLength(system_msg + developer_msg);
            size_t tools_tokens = 0;
            if (tools.is_array() && !tools.empty()) {
                std::string tools_str = tools.dump();
                tools_tokens = context_->TokenLength(tools_str);
            }
            PromptStatsHelper::PrintAfterPreFilterStats(msg, context_, system_tokens, contextSize, tools_tokens, &pre_filter_.GetStats(),
                                                        instance_config_->get_context_size(), /*is_harmony=*/true);

            ComputeAndPrintFitStats(optimized, context_, system_tokens, tools_tokens, contextSize);

            PromptStatsHelper::PrintFinalStats(ledger_final_tokens, result.length(), contextSize, has_tools,
                                               instance_config_->get_context_size());
        }

        FillLedger(ledger_tokens_in, ledger_final_tokens, ledger_messages_in, optimized);

        return result;
    }

    void FillLedger(size_t tokens_in, size_t tokens_out, size_t messages_in, const OptimizedMessages& optimized)
    {
        const auto& pf_stats = pre_filter_.GetStats();
        const auto& opt_stats = optimizer_.GetLastStats();
        last_ledger_.context_size = instance_config_->get_context_size();
        last_ledger_.tokens_in = tokens_in;
        last_ledger_.tokens_out = tokens_out;
        last_ledger_.messages_in = messages_in;
        last_ledger_.messages_kept = optimized.messages.size();
        last_ledger_.messages_dropped = pf_stats.total_dropped + optimized.dropped_count;
        last_ledger_.messages_truncated = pf_stats.truncated_count;
        last_ledger_.tools_tier = opt_stats.tools_tier;
        last_ledger_.tools_total = opt_stats.tools_total;
        last_ledger_.tools_kept = opt_stats.tools_kept;
        last_ledger_.skills_total = opt_stats.skills_total;
        last_ledger_.skills_kept = opt_stats.skills_kept;
        last_ledger_.skills_budget_tokens = opt_stats.skills_budget_tokens;
        last_ledger_.skills_l2 = opt_stats.skills_l2;
        last_ledger_.skills_l1 = opt_stats.skills_l1;
        last_ledger_.skills_l0 = opt_stats.skills_l0;
        last_ledger_.emergency_truncated = optimized.emergency_truncated;
        last_ledger_.memo_active = optimized.memo_active;
        last_ledger_.memo_confidence = optimized.memo_confidence;
        last_ledger_.memo_refresh_count = optimized.memo_refresh_count;
        last_ledger_.memo_pages_total = optimized.memo_pages_total;
        last_ledger_.messages_dropped_prefix_len = pf_stats.messages_dropped_prefix_len;
        last_ledger_.prefix_reset_reason = pf_stats.prefix_reset_reason;
    }

    void LogAgentType(AgentType agentType, const std::string& context_prefix)
    {
        if (agentType == AgentType::SUBAGENT) {
            My_Log{My_Log::Level::kInfo} << "[" << context_prefix << "] Agent type: SUBAGENT - applying lightweight optimization" << std::endl;
        } else if (agentType == AgentType::MAIN_AGENT) {
            My_Log{My_Log::Level::kInfo} << "[" << context_prefix << "] Agent type: MAIN_AGENT - applying full optimization" << std::endl;
        } else {
            My_Log{My_Log::Level::kInfo} << "[" << context_prefix << "] Agent type: UNKNOWN - applying full optimization (fallback)" << std::endl;
        }
    }

    AgentType DetectAgentTypeAndLog(const std::string& system_prompt, const std::string& context_prefix)
    {
        AgentType agentType = optimizer_.DetectAgentType(system_prompt);
        LogAgentType(agentType, context_prefix);
        return agentType;
    }

    std::string BuildRepeatNotesForTail() const
    {
        if (!request_data_.is_object() || !request_data_.contains("messages") || !request_data_["messages"].is_array())
            return "";
        const auto &cfg = instance_config_->i_model_config_.GetToolCallRepairConfig().redundant_call_guard;
        if (!cfg.enabled)
            return "";
        const auto& messages = request_data_["messages"];
        const std::string session_key = TaskMemoBuilder::ComputeFirstMsgKey(messages);
        const std::string key = ToolCallRepetitionStore::MakeKey(session_key, instance_config_->get_model_name());
        if (key.empty())
            return "";

        auto& store = ToolCallRepetitionStore::GetInstance();
        const std::string last_signature = store.GetLastSignature(key);
        std::string preview;
        int ledger_total = 0;
        bool digest_stable = true;
        if (!last_signature.empty())
            store.GetOutcomeSummary(key, last_signature, preview, ledger_total, digest_stable);

        std::vector<std::pair<std::string, std::string>> candidates;

        const int repeat_count = store.GetRepeatCount(key);
        if (cfg.repeat_threshold > 0 && repeat_count >= cfg.repeat_threshold) {
            candidates.emplace_back("sig:" + last_signature,
                                    TaskMemoBuilder::RenderRedundantToolCallNote(repeat_count, preview, digest_stable,
                                                                                  cfg.hard_block_threshold));
        }
        const int non_contiguous_count = store.GetNonContiguousRepeatCount(key);
        const int non_contiguous_threshold = ToolCallRepetitionStore::EffectiveNonContiguousNoticeThreshold(
            cfg.non_contiguous_repeat_threshold, cfg.hard_block_threshold);
        if (non_contiguous_threshold > 0 && non_contiguous_count >= non_contiguous_threshold) {

            candidates.emplace_back("sig:" + last_signature,
                                    TaskMemoBuilder::RenderNonContiguousRepeatNote(non_contiguous_count, preview,
                                                                                    digest_stable,
                                                                                    cfg.hard_block_threshold));
        }

        const auto entry = task_memo_builder_.Lookup(messages);
        if (entry && entry->contains("read_files") && (*entry)["read_files"].is_object()) {
            const std::string last_read_ref = ExtractLatestAssistantReadRef(messages);
            std::string reread_path;
            std::string note = TaskMemoBuilder::BuildRereadFromTopNote(last_read_ref, (*entry)["read_files"],
                                                                       reread_path);
            if (!note.empty())
                candidates.emplace_back("reread:" + reread_path, note);
        }
        if (entry) {
            const std::string overlap_path = entry->value("_overlap_reread_path", std::string(""));
            size_t next_offset = 0;
            if (!overlap_path.empty() && entry->contains("read_files") && (*entry)["read_files"].is_object()
                    && (*entry)["read_files"].contains(overlap_path)) {
                next_offset = task_memo_scan::NextUnreadOffset((*entry)["read_files"][overlap_path]);
            }
            std::string note = TaskMemoBuilder::BuildOverlapRereadNote(
                entry->value("overlap_reread_count", 0), entry->value("overlap_reread_threshold", 0),
                overlap_path, entry->value("_overlap_reread_range", std::string("")), next_offset);
            if (!note.empty())
                candidates.emplace_back("overlap:" + overlap_path, note);
        }

        for (const auto& candidate : candidates) {
            if (candidate.second.empty())
                continue;

            const uint64_t content_digest = static_cast<uint64_t>(std::hash<std::string>{}(candidate.second));
            if (store.TryMarkNoteSlot(key, candidate.first, content_digest))
                return "\n\n## Task Memo\n" + candidate.second;
        }
        return "";
    }

    std::string BuildConsecutiveToolFailureNote() const
    {
        if (!request_data_.is_object() || !request_data_.contains("messages") || !request_data_["messages"].is_array())
            return "";
        const auto &cfg = instance_config_->i_model_config_.GetToolCallRepairConfig().redundant_call_guard;
        if (!cfg.enabled || cfg.consecutive_failure_threshold <= 0)
            return "";
        const auto& messages = request_data_["messages"];
        int streak = 0;
        std::string tool_name;
        for (auto it = messages.rbegin(); it != messages.rend(); ++it) {
            const json& msg = *it;
            if (!msg.is_object())
                break;
            std::string role = msg.value("role", "");
            if (role == "assistant")
                continue;
            if (role != "tool")
                break;
            std::string content = get_json_value<std::string>(msg, "content", "");
            const bool bad = content.find_first_not_of(" \t\r\n") == std::string::npos ||
                             TaskMemoBuilder::LooksLikeToolFailure(content);
            if (!bad)
                break;
            ++streak;
            if (tool_name.empty())
                tool_name = msg.value("name", "");
        }
        if (streak < cfg.consecutive_failure_threshold)
            return "";
        std::ostringstream oss;
        oss << "Note: the last " << streak << " tool result(s)"
            << (tool_name.empty() ? "" : (" (e.g. `" + tool_name + "`)"))
            << " came back empty or failed. Stop retrying the same approach with minor variations; "
               "use a different tool or a path/fact you already confirmed, or report the blocker instead.\n";
        return "\n\n## Task Memo\n" + oss.str();
    }

    std::string BuildGoalReminder(int max_chars) const
    {
        if (!request_data_.is_object() || !request_data_.contains("messages") || !request_data_["messages"].is_array())
            return "";
        const auto entry = task_memo_builder_.Lookup(request_data_["messages"]);
        if (!entry || !entry->contains("original_goal_raw") || !(*entry)["original_goal_raw"].is_string())
            return "";
        std::string goal = safe_utf8_truncate((*entry)["original_goal_raw"].get<std::string>(), (size_t) max_chars, "...");
        for (char &c : goal)
            if (c == '\n' || c == '\r')
                c = ' ';
        if (goal.empty())
            return "";
        return "Original request: " + goal +
               "\nKeep satisfying it, or say why it can no longer be met and ask the user before changing it.\n";
    }

    static std::string CycleToolCallKey(const json &tool_call)
    {
        if (!tool_call.is_object() || !tool_call.contains("function") || !tool_call["function"].is_object())
            return "";
        const auto &fn = tool_call["function"];
        const std::string name = fn.value("name", std::string());
        if (name.empty())
            return "";
        std::string raw_args;
        if (fn.contains("arguments"))
        {
            if (fn["arguments"].is_string())
                raw_args = fn["arguments"].get<std::string>();
            else
                raw_args = fn["arguments"].dump();
        }
        nlohmann::json parsed = nlohmann::json::parse(raw_args, nullptr, false);
        const std::string args = parsed.is_discarded() ? raw_args : parsed.dump();
        return name + "\x1f" + args;
    }

    static std::vector<std::string> CollectCycleToolCallKeys(const json &messages)
    {
        std::vector<std::string> keys;
        if (!messages.is_array())
            return keys;
        for (const auto &message : messages)
        {
            if (!message.is_object() || message.value("role", "") != "assistant" ||
                !message.contains("tool_calls") || !message["tool_calls"].is_array())
                continue;
            for (const auto &tool_call : message["tool_calls"])
            {
                const std::string key = CycleToolCallKey(tool_call);
                if (!key.empty())
                    keys.push_back(key);
            }
        }
        return keys;
    }

    bool ShouldThinkThisTurn(bool *failure_trigger = nullptr, bool *forced_unblock_trigger = nullptr) const
    {
        const auto &at_cfg = instance_config_->i_model_config_.GetAdaptiveThinkingConfig();
        if (!at_cfg.enabled || at_cfg.time_budget_s <= 0)
            return false;
        if (!request_data_.is_object() || !request_data_.contains("messages") || !request_data_["messages"].is_array())
            return false;
        const auto &messages = request_data_["messages"];
        if (messages.empty() || !messages.back().is_object() || messages.back().value("role", "") != "tool")
            return false;
        const std::string session_key = TaskMemoBuilder::ComputeFirstMsgKey(messages);
        std::vector<std::string> recent_errors;
        for (size_t i = messages.size(); i-- > 0 && (int) recent_errors.size() <= at_cfg.error_repeat_window;)
        {
            if (!messages[i].is_object())
                continue;
            const std::string role = messages[i].value("role", "");
            if (role == "user")
                break;
            if (role == "tool")
                recent_errors.push_back(FirstFailureLine(get_json_value<std::string>(messages[i], "content", "")));
        }
        struct ThinkCandidate {
            std::string reason;
            std::string signature;
            bool failure;
        };
        std::vector<ThinkCandidate> candidates;
        if (!recent_errors.empty() && !recent_errors[0].empty())
        {
            std::string failure_reason;
            if (recent_errors.size() < 2 || recent_errors[1].empty())
                failure_reason = "new_failure";
            else
            {
                for (size_t i = 2; i < recent_errors.size(); ++i)
                {
                    if (recent_errors[i] == recent_errors[0])
                    {
                        failure_reason = "error_repeated";
                        break;
                    }
                }
                if (failure_reason.empty() && recent_errors[1] != recent_errors[0])
                    failure_reason = "changed_failure";
            }
            if (!failure_reason.empty())
                candidates.push_back({failure_reason, recent_errors[0], true});
        }
        if (at_cfg.near_repeat_window > 1)
        {
            const std::string near_tpl = NearRepeatTemplate(messages, at_cfg.near_repeat_window);
            if (!near_tpl.empty())
                candidates.push_back({"near_repeat", "near:" + near_tpl, false});
        }

        constexpr size_t kCycleWindow = 48;
        constexpr size_t kCycleRepeatThreshold = 4;
        bool cycle_checked = false;
        size_t cycle_repeats = 0;
        std::string cycle_signature;
        const std::vector<std::string> cycle_keys = CollectCycleToolCallKeys(messages);
        if (!cycle_keys.empty())
        {
            cycle_repeats = ToolCallRepetitionStore::CountRepeatsInWindow(cycle_keys, kCycleWindow);
            cycle_signature = BuildCycleRepeatSignature(cycle_keys, kCycleWindow);
            cycle_checked = true;
            if (cycle_repeats >= kCycleRepeatThreshold && !cycle_signature.empty())
                candidates.push_back({"cycle_repeat", cycle_signature, false});
        }
        const auto &rg_cfg = instance_config_->i_model_config_.GetToolCallRepairConfig().redundant_call_guard;
        std::string repeat_key;
        int repeat_count = 0;
        bool repeat_count_checked = false;
        if (rg_cfg.enabled)
        {
            repeat_key = ToolCallRepetitionStore::MakeKey(session_key, instance_config_->get_model_name());
            repeat_count = ToolCallRepetitionStore::GetInstance().GetRepeatCount(repeat_key);
            repeat_count_checked = true;
            if (repeat_count >= 2)
                candidates.push_back({"repeat_count=" + std::to_string(repeat_count), "repeat:" + repeat_key, false});
        }

        struct SessionThinkState {
            std::unordered_map<std::string, int> per_signature;

            std::unordered_map<std::string, int> consecutive_block_count;
            size_t last_think_msg_count = 0;
            int think_turns = 0;
            int tool_turns = 0;

            int failure_reserve_used = 0;

            std::string last_cycle_signature;
            std::chrono::steady_clock::time_point touched;
        };
        static std::mutex s_mu;
        static std::unordered_map<std::string, SessionThinkState> s_state;
        const auto now = std::chrono::steady_clock::now();
        std::lock_guard<std::mutex> lk(s_mu);
        if (s_state.size() > 512)
            s_state.clear();
        auto &st = s_state[session_key];
        if (now - st.touched >= std::chrono::hours(2))
            st = SessionThinkState{};
        st.touched = now;
        ++st.tool_turns;
        if (repeat_count_checked && repeat_count < 2)
        {
            const auto it = st.per_signature.find("repeat:" + repeat_key);
            if (it != st.per_signature.end())
                it->second = 0;
        }
        if (cycle_checked)
        {
            if (cycle_repeats >= kCycleRepeatThreshold && !cycle_signature.empty())
            {
                st.last_cycle_signature = cycle_signature;
            }
            else if (!st.last_cycle_signature.empty())
            {

                const auto it = st.per_signature.find(st.last_cycle_signature);
                if (it != st.per_signature.end())
                    it->second = 0;
                st.last_cycle_signature.clear();
            }
        }
        bool fallback_paced = false;
        for (const auto &candidate : candidates)
        {
            const bool new_signature = st.per_signature.find(candidate.signature) == st.per_signature.end();
            const bool paced = fallback_paced || !new_signature || candidate.reason == "changed_failure";
            int &sig_count = st.per_signature[candidate.signature];
            std::string blocked;
            if (at_cfg.max_per_signature > 0 && sig_count >= at_cfg.max_per_signature)
                blocked = "signature_limit";
            else if (paced && st.last_think_msg_count > 0 && messages.size() <= st.last_think_msg_count + 2)
                blocked = "cooldown";
            else if (paced && at_cfg.max_think_turn_share > 0.0 &&
                     st.think_turns >= static_cast<int>(at_cfg.max_think_turn_share * st.tool_turns))
                blocked = "turn_share";

            bool forced_unblock = false;
            bool reserve_unblock = false;
            if (blocked == "signature_limit" || blocked == "cooldown")
            {
                forced_unblock = ComputeForcedUnblockFromStreak(
                    st.consecutive_block_count[candidate.signature], at_cfg.max_consecutive_block);
            }
            else if (blocked == "turn_share" && IsReserveEligibleThinkReason(candidate.reason))
            {

                reserve_unblock = TryConsumeFailureThinkReserve(
                    st.failure_reserve_used, at_cfg.reserved_failure_think_turns);
            }

            if (!blocked.empty() && !forced_unblock && !reserve_unblock)
            {
                My_Log{} << "[AdaptiveThinking] skip: " << candidate.reason << " blocked_by=" << blocked
                         << " (sig_count=" << sig_count << ")" << std::endl;
                fallback_paced = true;
                continue;
            }
            if (forced_unblock)
            {
                My_Log{} << "[AdaptiveThinking] force-allow: " << candidate.reason << " blocked_by=" << blocked
                         << " hit " << at_cfg.max_consecutive_block
                         << " consecutive blocks, bypassing once and resetting streak (sig_count=" << sig_count
                         << ")" << std::endl;
            }
            else
            {
                if (reserve_unblock)
                {
                    My_Log{} << "[AdaptiveThinking] reserve-allow: " << candidate.reason
                             << " blocked_by=turn_share, drawing on the failure think reserve ("
                             << st.failure_reserve_used << "/" << at_cfg.reserved_failure_think_turns
                             << ")" << std::endl;
                }

                st.consecutive_block_count.erase(candidate.signature);
            }
            ++sig_count;
            ++st.think_turns;
            st.last_think_msg_count = messages.size();
            if (failure_trigger)
                *failure_trigger = candidate.failure;
            if (forced_unblock_trigger)
                *forced_unblock_trigger = forced_unblock || reserve_unblock;
            My_Log{} << "[AdaptiveThinking] enabling /think for this turn: " << candidate.reason
                     << " (sig_count=" << sig_count << ", think_turns=" << st.think_turns
                     << "/" << st.tool_turns << ")" << std::endl;
            return true;
        }
        return false;
    }

    static std::string NearRepeatTemplate(const json &messages, int window)
    {
        std::vector<std::string> calls;
        for (size_t i = messages.size(); i-- > 0 && (int) calls.size() < window;)
        {
            const json &m = messages[i];
            if (!m.is_object())
                continue;
            const std::string role = m.value("role", "");
            if (role == "user")
                break;
            if (role != "assistant" || !m.contains("tool_calls") || !m["tool_calls"].is_array() || m["tool_calls"].empty())
                continue;
            const json &fn = m["tool_calls"][0].value("function", json::object());
            const json &args = fn.contains("arguments") ? fn["arguments"] : json();
            calls.push_back(fn.value("name", "") + "\x1f" + (args.is_string() ? args.get<std::string>() : args.dump()));
        }
        if ((int) calls.size() < window)
            return "";
        size_t lcp = calls[0].size();
        bool all_same = true;
        for (size_t k = 1; k < calls.size(); ++k)
        {
            size_t n = 0;
            while (n < lcp && n < calls[k].size() && calls[k][n] == calls[0][n])
                ++n;
            lcp = n;
            all_same = all_same && calls[k] == calls[0];
        }
        size_t shortest = calls[0].size();
        for (const auto &c : calls)
            shortest = std::min(shortest, c.size());
        if (all_same || lcp < 24 || lcp * 10 < shortest * 6)
            return "";
        return calls[0].substr(0, std::min<size_t>(lcp, 160));
    }

    static std::string FirstFailureLine(const std::string &content)
    {
        const std::string line = TaskMemoBuilder::FirstFailureLine(content);
        std::string normalized;
        normalized.reserve(line.size());
        bool in_digits = false;
        for (char c : line)
        {
            if (std::isdigit(static_cast<unsigned char>(c)))
            {
                if (!in_digits)
                    normalized += '#';
                in_digits = true;
            }
            else
            {
                normalized += c;
                in_digits = false;
            }
        }
        return normalized.substr(0, 160);
    }

    std::string BuildTaskMemoSection() const
    {
        if (!request_data_.is_object() || !request_data_.contains("messages") || !request_data_["messages"].is_array()) {
            return "";
        }
        std::string consecutive_failure_note = BuildConsecutiveToolFailureNote();

        std::string repeat_note = BuildRepeatNotesForTail();

        auto memo_entry = task_memo_builder_.Lookup(request_data_["messages"]);
        if (!memo_entry)
            return repeat_note + consecutive_failure_note;

        std::string rendered = TaskMemoBuilder::Render(*memo_entry);
        if (rendered.empty())
            return repeat_note + consecutive_failure_note;

        const auto& po_cfg = instance_config_->i_model_config_.GetPromptOptimizationConfig();
        double ratio = po_cfg.task_memo.token_budget_ratio;
        if (ratio > 0.0 && !rendered.empty()) {
            double bytes_per_token = EstimateCjkAwareBytesPerToken(
                rendered, po_cfg.fidelity.cjk_bytes_per_token, po_cfg.fidelity.ascii_bytes_per_token);
            size_t budget_bytes = static_cast<size_t>(
                instance_config_->get_context_size() * ratio * bytes_per_token);
            if (budget_bytes > 0 && rendered.size() > budget_bytes) {
                rendered = TaskMemoBuilder::Render(*memo_entry, budget_bytes);
            }
        }
        return "\n\n" + rendered + repeat_note + consecutive_failure_note;
    }

    std::string BuildTaskMemoBody() const
    {
        if (!request_data_.is_object() || !request_data_.contains("messages") || !request_data_["messages"].is_array())
            return "";
        auto memo_entry = task_memo_builder_.Lookup(request_data_["messages"]);
        if (!memo_entry)
            return "";
        std::string rendered = TaskMemoBuilder::Render(*memo_entry);
        if (rendered.empty())
            return "";
        const auto& po_cfg = instance_config_->i_model_config_.GetPromptOptimizationConfig();
        double ratio = po_cfg.task_memo.token_budget_ratio;
        if (ratio > 0.0 && !rendered.empty()) {
            double bytes_per_token = EstimateCjkAwareBytesPerToken(
                rendered, po_cfg.fidelity.cjk_bytes_per_token, po_cfg.fidelity.ascii_bytes_per_token);
            size_t budget_bytes = static_cast<size_t>(
                instance_config_->get_context_size() * ratio * bytes_per_token);
            if (budget_bytes > 0 && rendered.size() > budget_bytes) {
                rendered = TaskMemoBuilder::Render(*memo_entry, budget_bytes);
            }
        }
        return "\n\n" + rendered;
    }

    std::string BuildTaskMemoNotes() const
    {

        return ComposeTaskMemoNotes(false, BuildRepeatNotesForTail(), BuildConsecutiveToolFailureNote());
    }

    OptimizedMessages ApplyFitMessagesToContext(
        const std::vector<GenieChatMessage>& messages,
        const std::string& system_prompt,
        int contextSize,
        const std::string& log_prefix)
    {
        My_Log{My_Log::Level::kDebug} << "[" << log_prefix << "] Fitting messages to context..." << std::endl;

        const json* raw_messages_for_memo = nullptr;
        if (request_data_.is_object() && request_data_.contains("messages") && request_data_["messages"].is_array()) {
            raw_messages_for_memo = &request_data_["messages"];
        }

        auto optimized = pre_filter_.FitMessagesToContext(messages, system_prompt, contextSize,
                                                            MessageCompressionConfig(),
                                                            raw_messages_for_memo, &task_memo_builder_);

        if (!optimized.success) {
            My_Log{My_Log::Level::kError}
                << "[" << log_prefix << "] Failed: " << optimized.error_message << std::endl;
            throw ReportError{std::move(optimized.error_message)};
        }

        My_Log{My_Log::Level::kInfo} << "[" << log_prefix << "] Success - Messages: "
                                      << optimized.messages.size()
                                      << ", Tokens: " << optimized.total_tokens << std::endl;
        AnnotateRepeatedReads(optimized, messages);
        return optimized;
    }

    void AnnotateRepeatedReads(OptimizedMessages& optimized, const std::vector<GenieChatMessage>& full_messages) const
    {
        std::unordered_set<std::string> seen_paths;
        std::unordered_set<const GenieChatMessage*> duplicate_messages;
        std::deque<std::string> pending_tool_calls;
        for (const auto& m : full_messages) {
            if (m.role == "assistant") {
                if (m.content.find("<tool_call>") != std::string::npos ||
                    m.content.find("to=functions.") != std::string::npos)
                    pending_tool_calls.push_back(m.content);
            } else if (m.role == "tool") {
                std::string call_content;
                if (!pending_tool_calls.empty()) {
                    call_content = std::move(pending_tool_calls.front());
                    pending_tool_calls.pop_front();
                }
                std::string path = TaskMemoBuilder::ExtractReadToolPath(call_content);
                if (path.empty())
                    continue;
                if (seen_paths.count(path)) {
                    duplicate_messages.insert(&m);
                } else {
                    seen_paths.insert(path);
                }
            }
        }

        if (optimized.messages.size() > full_messages.size())
            return;
        const size_t full_offset = full_messages.size() - optimized.messages.size();
        for (size_t optimized_index = 0; optimized_index < optimized.messages.size(); ++optimized_index) {
            auto& m = optimized.messages[optimized_index];
            if (m.role != "tool")
                continue;
            const size_t full_index = full_offset + optimized_index;
            if (full_index >= full_messages.size())
                continue;
            if (full_messages[full_index].role != m.role) {
                My_Log{My_Log::Level::kWarning}
                    << "[AnnotateRepeatedReads] Message alignment role mismatch at optimized_index="
                    << optimized_index << std::endl;
                continue;
            }
            if (duplicate_messages.count(&full_messages[full_index])) {
                m.content += "\n\n[Note: this file was already read at an earlier turn; its result may still be "
                             "present in the conversation or summarized in the Task Memo above. Check "
                             "those before re-reading it unless the file may have changed since then.]";
            }
        }
    }

    void ComputeAndPrintFitStats(
        const OptimizedMessages& optimized,
        std::shared_ptr<ContextBase> model_handle,
        size_t system_tokens,
        size_t tools_tokens,
        int contextSize)
    {

        PromptStatsHelper::PrintAfterFitStatsFromOptimized(
            optimized.messages, model_handle,
            system_tokens, tools_tokens,
            contextSize, instance_config_->get_context_size());
    }

    std::string ExtractFinalChannel(const std::string& harmony_msg) const
    {
        static const std::string kFinalChannelMarker = "<|channel|>final<|message|>";
        static const std::string kEndMarker = "<|end|>";
        static const std::string kReturnMarker = "<|return|>";

        std::string result;
        size_t pos = 0;

        while (true) {
            size_t final_pos = harmony_msg.find(kFinalChannelMarker, pos);
            if (final_pos == std::string::npos) {
                break;
            }

            size_t start_pos = harmony_msg.rfind("<|start|>assistant", final_pos);
            if (start_pos == std::string::npos) {
                break;
            }

            size_t content_start = final_pos + kFinalChannelMarker.length();

            size_t content_end = harmony_msg.find(kEndMarker, content_start);
            size_t end_marker_len = kEndMarker.length();
            if (content_end == std::string::npos) {
                content_end = harmony_msg.find(kReturnMarker, content_start);
                end_marker_len = kReturnMarker.length();
                if (content_end == std::string::npos) {
                    break;
                }
            }

            std::string content = harmony_msg.substr(content_start, content_end - content_start);

            result += "<|start|>assistant" + kFinalChannelMarker + content + kEndMarker;

            pos = content_end + end_marker_len;
        }

        return result;
    }

    bool IsToolCallMessage(const std::string& harmony_msg) const
    {
        return harmony_msg.find("to=functions.") != std::string::npos;
    }

    std::string getCurrentDate() const
    {
        auto now = std::chrono::system_clock::now();
        auto time_t = std::chrono::system_clock::to_time_t(now);

        std::tm tm{};
#ifdef _WIN32
        localtime_s(&tm, &time_t);
#else
        localtime_r(&time_t, &tm);
#endif

        std::ostringstream oss;
        oss << std::put_time(&tm, "%Y-%m-%d");
        return oss.str();
    }

    json SamplerFallback(const std::string &key) const
    {
        auto &sampler = instance_config_->sampler();
        return (sampler.contains(key) && sampler.at(key).is_number()) ? sampler.at(key) : json();
    }

    std::string RunSummarizationInference(const std::string& prompt,
                                          ContextBase::PrefillHeartbeatCallback prefill_heartbeat = nullptr)
    {
        if (prompt.empty()) return "";

        ModelInput sum_input;
        sum_input.text_ = prompt;

        {
            int context_size = instance_config_->get_context_size();
            size_t prompt_tokens = context_->TokenLength(prompt);
            int available_output = context_size - static_cast<int>(prompt_tokens);

            available_output = std::max(available_output, 512);
            available_output = std::min(available_output, context_size);

            json params;
            json temp = SamplerFallback("temp");
            if (temp.is_number()) params["temp"] = temp;
            json top_k = SamplerFallback("top-k");
            if (top_k.is_number()) params["top_k"] = top_k;
            json top_p = SamplerFallback("top-p");
            if (top_p.is_number()) params["top_p"] = top_p;
            context_->SetParamsByConfig(params);
        }

        std::string result;
        bool success = false;

        static constexpr auto kSummarizationInferenceTimeout = std::chrono::seconds(300);
        const auto infer_start_time = std::chrono::steady_clock::now();
        bool timed_out = false;

        try
        {
            success = context_->Query(
                sum_input,
                [&result, &timed_out, infer_start_time](std::string& token) -> bool {

                    if (!token.empty())
                    {
                        My_Log{}.original(true) << token;
                    }
                    result += token;
                    if (std::chrono::steady_clock::now() - infer_start_time > kSummarizationInferenceTimeout)
                    {
                        timed_out = true;
                        return false;
                    }
                    return true;
                },
                prefill_heartbeat
            );
        }
        catch (const std::exception& e)
        {
            My_Log{My_Log::Level::kWarning}
                << "[RunSummarizationInference] Query threw exception: "
                << e.what() << std::endl;
            return "";
        }
        catch (...)
        {
            My_Log{My_Log::Level::kWarning}
                << "[RunSummarizationInference] Query threw unknown exception" << std::endl;
            return "";
        }

        {
            My_Log{} << "--- [Summarization] Token Summary Start ---" << std::endl;
            auto profile = context_->HandleProfile();
            if (!profile.empty())
            {
                try
                {
                    My_Log{} << "Time to First Token: "
                             << std::fixed << std::setprecision(2)
                             << profile.at("time_to_first_token").get<std::string>()
                             << " s" << std::endl;
                    My_Log{} << "Token Generation Time: "
                             << std::fixed << std::setprecision(2)
                             << profile.at("token_generation_time").get<std::string>()
                             << " s" << std::endl;
                    My_Log{} << "Num Prompt Tokens: "
                             << profile.at("num_prompt_tokens")
                             << ", Prompt Length: " << prompt.size() << " chars" << std::endl;
                    My_Log{} << "Prompt Processing Rate: "
                             << std::fixed << std::setprecision(2)
                             << profile.at("prompt_processing_rate").get<std::string>()
                             << " toks/sec" << std::endl;
                    My_Log{} << "Num Generated Tokens: "
                             << profile.at("num_generated_tokens")
                             << ", Summary Length: " << result.size() << " chars" << std::endl;
                    My_Log{} << "Token Generation Rate: "
                             << std::fixed << std::setprecision(2)
                             << profile.at("token_generation_rate").get<std::string>()
                             << " toks/sec" << std::endl;
                }
                catch (const std::exception& e)
                {
                    My_Log{My_Log::Level::kWarning}
                        << "[RunSummarizationInference] PrintProfile failed: " << e.what() << std::endl;
                }
            }
            My_Log{} << "--- [Summarization] Token Summary End ---" << std::endl;
        }

        if (timed_out)
        {
            My_Log{My_Log::Level::kWarning}
                << "[RunSummarizationInference] Inference exceeded "
                << kSummarizationInferenceTimeout.count() << "s budget ("
                << result.size() << " chars generated so far, likely stuck mid-<think> "
                << "self-correction loop), aborting and falling back to original content"
                << std::endl;
            return "";
        }

        if (!success)
        {
            My_Log{My_Log::Level::kWarning}
                << "[RunSummarizationInference] Query returned false, result so far ("
                << result.size() << " chars): "
                << (result.empty() ? "<empty>" : result.substr(0, std::min(result.size(), size_t(200))))
                << std::endl;
            return "";
        }

        My_Log{My_Log::Level::kWarning}
            << "[RunSummarizationInference] Full output (" << result.size() << " chars):\n"
            << result << std::endl;

        My_Log{My_Log::Level::kInfo}
            << "[RunSummarizationInference] Query succeeded, result ("
            << result.size() << " chars)" << std::endl;

        return result;
    }

    void Reset()
    {
        model_input_.system_.clear();
        model_input_.text_.clear();
        model_input_.image_.clear();
        model_input_.audio_.clear();
        model_input_.agent_type_ = "sub";
        tool_call_id_to_name_.clear();
        last_ledger_ = PromptLedger{};
    }

    static inline const std::string BLANK_STRING;

    ChatHistory &chat_history_;
    ModelInput model_input_;
    ModelInstanceConfig *instance_config_;
    std::shared_ptr<ContextBase> context_;
    PromptOptimizer optimizer_;
    MessagePreFilter pre_filter_;

    TaskMemoBuilder task_memo_builder_;

    std::unordered_map<std::string, std::string> tool_call_id_to_name_;

    json request_data_;

    PromptLedger last_ledger_;
};

#endif
