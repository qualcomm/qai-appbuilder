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


using json = nlohmann::ordered_json;

class ModelInputBuilder
{
public:
    ModelInputBuilder(ChatHistory &chat_history, ModelInstanceConfig *instance_config, const json &request_data = nullptr)
        : chat_history_{chat_history},
          instance_config_{instance_config},
          context_{instance_config_->i_model_config_.get_genie_model_handle().lock()},
          optimizer_{instance_config_->i_model_config_, context_.get()},
          pre_filter_{instance_config_->i_model_config_, context_.get()}
    {
        request_data_ = request_data;
    }

    // is_alive_fn：可选的连接存活检测回调，用于 Phase -1 摘要推理期间检测客户端是否断开。
    // stream 路径传入 [&sink]() { return sink.is_writable(); }，非 stream 路径传入 nullptr。
    ModelInput &Build(json &data, bool &is_tool,
                      std::function<bool()> is_alive_fn = nullptr)
    {
        Reset();

        // Sanitize incoming message contents to prevent null bytes (\0)
        // and other control characters from causing truncation or memory issues
        if (data.contains("messages") && data["messages"].is_array()) {
            for (auto& e : data["messages"]) {
                if (e.contains("content") && e["content"].is_string()) {
                    std::string content = e["content"].get<std::string>();
                    clean_control_characters_inplace(content);
                    e["content"] = content;
                }
            }
        }

        // 修复：检查 messages 字段是否存在且为 array，避免对 null/非 array 类型迭代触发异常
        // 注意：第50行的 sanitize 循环已经检查了 data.contains("messages") && data["messages"].is_array()，
        // 但如果 messages 不存在，第50行的检查会跳过 sanitize，而此处的迭代仍会执行，
        // 对 null 类型的 json 迭代会触发 type_error.302
        if (!data.contains("messages") || !data["messages"].is_array()) {
            throw ReportError{std::string{"messages field is missing or not an array in request"}};
        }

        for (auto &e: data["messages"])
        {
            if (e["role"] == "user")
            {
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

        if (!model_input_.image_.empty() || !model_input_.audio_.empty())
        {
            return model_input_;
        }

        // 修复：使用 instance_config_（per-model 的 ModelInstanceConfig）而非 model_config_（全局 IModelConfig）
        // 在多模型场景下，model_config_ 的 get_prompt_type()/context_size() 等方法读取的是
        // 全局 IModelConfig 的状态（即最后加载的单模型），会导致所有请求使用同一个模型的配置，
        // 破坏多模型路由的正确性。
        bool is_harmony = (instance_config_->get_prompt_type() == PromptType::Harmony);

        // 为输出预留空间：取 minOutputNum 和 output_reserve_ratio 中的较大值
        // output_reserve_ratio 从 service_config.json 的 prompt_optimization 节加载（默认 0.20）
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

        // ── 长文本摘要化 ──────────────────────────────────────────
        // 在 prompt 构建前，对超长 user 消息和末尾 tool 消息链执行 Map-Reduce 摘要，
        // 以减少本地输入溢出概率。摘要失败时静默降级，保留原文继续走现有流程。
        {
            const auto& sum_cfg = po_cfg.long_text_summarization;
            if (instance_config_->getnumResponse() == -1
                    && sum_cfg.enabled
                    && data.contains("messages")
                    && data["messages"].is_array())
            {
                auto infer_fn = [this, &is_alive_fn](const std::string& prompt) -> std::string {
                    // 将 is_alive_fn 作为 prefill heartbeat 传入：
                    // 在摘要推理的 prefill 阶段（处理 prompt token 期间）定期调用，
                    // 向客户端发送保活帧，防止 httpx read 超时（默认 300s）。
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
                    is_alive_fn  // 连接存活检测：stream 路径传入 sink.is_writable()，非 stream 为 nullptr
                );

                summarizer.ProcessMessages(data["messages"]);
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

    std::string BuildPrompt(json &data, bool &is_tool, int contextSize)
    {
        is_tool = false;
        // 修复：检查 messages 字段是否存在且为 array
        if (!data.contains("messages") || !data["messages"].is_array()) {
            throw ReportError{std::string{"messages field is missing or not an array in BuildPrompt"}};
        }
        json msg = data["messages"];
        // 使用 contains 检查避免 nlohmann::json 在键不存在时静默插入 null 值
        json tools = (data.contains("tools") && data["tools"].is_array())
                     ? data["tools"] : json::array();

        // optimize_prompt: 当 numResponse == -1（参数 n==-1）时，启用所有压缩优化逻辑
        // 包括系统提示词优化、工具定义优化、消息预过滤（PreFilterMessages）和消息适配（FitMessagesToContext）
        // 修复：使用 instance_config_（per-model）而非 model_config_（全局 IModelConfig）
        bool optimize_prompt = (instance_config_->getnumResponse() == -1);

        std::string userToolsPrompt = "";
        std::string systemDefaultPrompt = "You are a helpful assistant.";
        std::string startDefaultPrompt;

        My_Log{My_Log::Level::kDebug} << "[Context] Window size: " << instance_config_->get_context_size() << " tokens" << std::endl;

        systemDefaultPrompt = model_input_.system_;
        
        // 收集优化前统计信息（延迟打印，避免被 PreFilterMessages 日志打断）
        // 修复：使用注入的 context_（多模型并发安全），而非 model_config_.get_genie_model_handle()
        // 在多模型场景下，get_genie_model_handle() 返回的是全局单模型句柄，会导致所有请求
        // 使用同一个模型句柄进行 TokenLength 计算，破坏多模型路由的正确性。
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
            // 使用注入的 context_ 引用，包装为 shared_ptr（不拥有所有权）
            // 注意：context_ 的生命周期由 ChatRequestHandler::ChatCompletions 中的 handle（shared_ptr）保证
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

        // 优化系统提示词（在 PreFilterMessages 之前，只在 optimize_prompt 时执行）
        if (optimize_prompt)
        {
            My_Log{My_Log::Level::kDebug} << "[Optimization] Enabled (n=-1)" << std::endl;

            AgentType agentType = DetectAgentTypeAndLog(systemDefaultPrompt, "Optimization");
            // 将 agent 类型写入 ModelInput，供底层推理日志标记使用
            model_input_.agent_type_ = (agentType == AgentType::MAIN_AGENT) ? "main" : "sub";

            if (agentType == AgentType::SUBAGENT) {
                systemDefaultPrompt = optimizer_.OptimizeSubagentSystemPrompt(systemDefaultPrompt, request_data_);
            } else {
                systemDefaultPrompt = optimizer_.OptimizeSystemPrompt(systemDefaultPrompt, request_data_);
            }

            auto stats = optimizer_.GetLastStats();
            My_Log{My_Log::Level::kInfo} << "[Optimization] System prompt savings: " << stats.savings_percent << "%" << std::endl;
        }

        // 必须在 PreFilterMessages 之前处理工具定义，确保 token 预算计算包含工具定义的 tokens
        std::string raw_tools_str;  // 原始 tools JSON 字符串（未经 OptimizeToolsPrompt 处理）
        if (tools.is_array() && !tools.empty())
        {
            is_tool = true;
            raw_tools_str = tools.dump();
            userToolsPrompt = raw_tools_str;
        }

        // 处理工具定义（如果存在），提前优化并追加到 systemDefaultPrompt，
        // 用于 PreFilterMessages 的 token 计算
        if (is_tool && !userToolsPrompt.empty()) {
            // [Refactor] 从 instance_config 获取工具提示词模板
            std::string tool_tmpl = instance_config_->get_tool_prompt_template();

            if (optimize_prompt) {
                userToolsPrompt = optimizer_.OptimizeToolsPrompt(userToolsPrompt, tool_tmpl);
            } else {
                userToolsPrompt = str_replace(tool_tmpl, "{tool_descs}", userToolsPrompt);
            }
            userToolsPrompt += "\n\n";
            systemDefaultPrompt += userToolsPrompt;
        }

        // 必须在 PreFilterMessages 之前追加 /think 或 /no_think，
        // 确保两阶段（PreFilterMessages 和 FitMessagesToContext）使用相同的 system prompt 进行 token 计算。
        // 按需求：只要当前模型是 instance_config_->is_thinking_model()，无论主推理还是其它场景，
        // 都必须统一追加 /think 或 /no_think；当关闭 thinking 时，还必须同时注入 FILL_THINK。
        if (instance_config_->is_thinking_model())
        {
            if (instance_config_->getenableThinking())
            {
                systemDefaultPrompt += "/think";
            }
            else
            {
                systemDefaultPrompt += "/no_think";
                startDefaultPrompt += FILL_THINK;
            }
        }

        // ========== 消息预过滤 ==========
        // 只在 optimize_prompt（n==-1）时执行压缩过滤
        if (optimize_prompt) {
            msg = pre_filter_.PreFilterMessages(msg, contextSize, systemDefaultPrompt, /*is_harmony=*/false, &tool_call_id_to_name_);
        }

        std::vector<GenieChatMessage> all_messages;
        all_messages.push_back({"user", model_input_.text_});

        // 第一遍：收集所有消息
        for (auto &element: msg)
        {
            auto role = get_json_value(element, "role", BLANK_STRING);
            std::string content;

            if (role == "assistant")
            {
                // 检查是否有 tool_calls 字段（OpenAI 标准格式）
                if (element.contains("tool_calls") && !element["tool_calls"].is_null() &&
                    element["tool_calls"].is_array())
                {
                    auto converted_calls = optimizer_.ConvertOpenAIToolCalls(element["tool_calls"]);
                    for (const auto& converted_content : converted_calls)
                    {
                        all_messages.push_back({role, converted_content});
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
            // system 消息已在前面处理，这里跳过
        }

        chat_history_.Clear();
        OptimizedMessages optimized;
        if (optimize_prompt) {
            optimized = ApplyFitMessagesToContext(
                all_messages,
                systemDefaultPrompt,
                contextSize,
                "Optimization"
            );
            for (const auto& opt_msg : optimized.messages) {
                chat_history_.AddMessage(opt_msg.role, opt_msg.content);
            }
        } else {
            for (const auto& msg_item : all_messages) {
                chat_history_.AddMessage(msg_item.role, msg_item.content);
            }
            optimized.messages = all_messages;
            optimized.success = true;
            optimized.total_tokens = 0;
        }

        // build model input
        // 修复：使用 instance_config_（per-model）的 prompt template，而非 model_config_（全局 IModelConfig）
        auto &j = instance_config_->get_prompt_template();

        // 修复：检查 prompt_template 是否有效（非 null 且为 object 类型）
        // 若 prompt.json 读取失败（文件不存在、格式错误、file.good()=false 等），
        // prompt_template 会保持 null，对 null 类型的 const json 使用字符串键会抛出 type_error.305。
        // 此处提前检查并抛出明确的错误信息，便于诊断。
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

        std::string modelInputContent = chat_history_.GetUserMessage(
                                                          str_replace(j["system"], "string", systemDefaultPrompt),
                                                          j["start"].get<std::string>() + startDefaultPrompt);

        // 计算最终提示词的真实 token 数
        // 修复：使用注入的 context_（多模型并发安全），而非 model_config_.get_genie_model_handle()
        // 直接使用 context_ 引用调用 TokenLength，无需通过 weak_ptr 获取 shared_ptr

        {
            size_t final_tokens = context_->TokenLength(modelInputContent);

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

            if (optimize_prompt && instance_config_->getenablePromptDebug()) {
                // 打印详细的消息列表（PreFilter 后的原始 JSON 消息，与 Harmony 路径对齐）
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

                    // 打印 tool_calls（assistant 消息）
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

                // 1. 打印优化前统计信息
                if (before_opt_stats.valid) {
                    PromptStatsHelper::PrintBeforeOptimizationStats(
                        before_opt_stats.messages_snapshot,
                        before_opt_stats.system_prompt_snapshot,
                        before_opt_stats.handle,
                        before_opt_stats.context_size_val,
                        before_opt_stats.tools_ptr,
                        before_opt_stats.full_context_size_val);
                }

                // 2. 打印 PreFilter 优化后统计信息
                size_t system_tokens = context_->TokenLength(systemDefaultPrompt);
                size_t tools_tokens = 0;
                if (!raw_tools_str.empty()) {
                    tools_tokens = context_->TokenLength(raw_tools_str);
                }
                PromptStatsHelper::PrintAfterPreFilterStats(msg, context_, system_tokens, contextSize, tools_tokens, &pre_filter_.GetStats(),
                                                            instance_config_->get_context_size());

                // 3. 打印 FitMessagesToContext 优化后统计信息
                ComputeAndPrintFitStats(optimized, context_, system_tokens, tools_tokens, contextSize);

                // 4. 打印最终提示词统计信息
                PromptStatsHelper::PrintFinalStats(final_tokens, modelInputContent.length(), contextSize, is_tool,
                                                   instance_config_->get_context_size());
            }

        }

        return modelInputContent;
    }

    // ========== Harmony 格式构建方法 ==========

    std::string BuildHarmonyPrompt(json &data, bool &is_tool, int contextSize)
    {
        const IModelConfig & model_config{instance_config_->i_model_config_};
        is_tool = false;

        // 修复：检查 messages 字段是否存在且为 array
        if (!data.contains("messages") || !data["messages"].is_array()) {
            throw ReportError{std::string{"messages field is missing or not an array in BuildHarmonyPrompt"}};
        }
        json msg = data["messages"];
        // 使用 contains 检查避免 nlohmann::json 在键不存在时静默插入 null 值
        json tools = (data.contains("tools") && data["tools"].is_array())
                     ? data["tools"] : json::array();

        bool has_tools = tools.is_array() && !tools.empty();
        if (has_tools) {
            is_tool = true;
        }

        std::string current_date = getCurrentDate();

        auto &j = instance_config_->get_prompt_template();
        std::string reasoning_level = j.value("reasoning_level", "medium");
        std::string knowledge_cutoff = j.value("knowledge_cutoff", "2024-06");

        // 从消息中提取 system 指令
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

        // optimize_prompt: 当 numResponse == -1（参数 n==-1）时，启用所有压缩优化逻辑
        bool optimize_prompt = (instance_config_->getnumResponse() == -1);

        std::string system_msg;
        std::string developer_msg;

        if (optimize_prompt) {
            My_Log{My_Log::Level::kDebug} << "[Harmony] System prompt optimization enabled (n=-1)" << std::endl;

            AgentType agentType = DetectAgentTypeAndLog(instructions, "Harmony");
            // 将 agent 类型写入 ModelInput，供底层推理日志标记使用
            model_input_.agent_type_ = (agentType == AgentType::MAIN_AGENT) ? "main" : "sub";

            if (agentType == AgentType::SUBAGENT) {
                // 子 agent：与 General 格式的 SubAgent 处理逻辑保持一致：
                //   Step 1: OptimizeSubagentSystemPrompt 重建 Skill Catalog + Few-shot，
                //           并通过 subagent_prompt_sections 保留原始 system prompt 中的
                //           SubAgent 特有段落（## Workspace / ## Subagent Context / ## Runtime 等）
                //   Step 2: 追加 Tools（TypeScript namespace 格式，与 MainAgent 相同）
                // 注意：OptimizeHarmonyDeveloperMessage 使用 prompt_sections（MainAgent 配置），
                //       而 OptimizeSubagentSystemPrompt 使用 subagent_prompt_sections（SubAgent 专用配置），
                //       两者的段落过滤规则不同，不能混用。
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
                        // Tool Usage Guidelines（与 OptimizeHarmonyDeveloperMessage 保持一致）
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
                        developer_msg += optimizer_.ConvertToolsToOptimizedTypeScript(tools);
                        developer_msg += "\n} // namespace functions";
                    }
                    developer_msg += "<|end|>";
                }
            } else {
                // 主 agent 或未知类型：使用完整优化逻辑
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
        } else {
            // 使用标准的 Harmony 格式（未优化）
            system_msg = HarmonyProcessor::BuildSystemMessage(
                knowledge_cutoff,
                current_date,
                reasoning_level,
                has_tools
            );

            if (has_tools || !instructions.empty()) {
                developer_msg = HarmonyProcessor::BuildDeveloperMessage(
                    instructions,
                    tools
                );
            }
        }

        // ========== 预扫描：建立完整的 tool_call_id → 函数名映射 ==========
        // 必须在 PreFilterMessages 之前对原始 msg 进行扫描，确保即使后续过滤
        // 丢弃了某些 assistant 消息，tool 消息仍能通过映射找到对应的函数名
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

        // 保存优化前统计所需的数据快照（msg 在 PreFilterMessages 后会被修改）
        json before_opt_msg_snapshot = msg;
        // ========== 消息预过滤 ==========
        // 只在 optimize_prompt（n==-1）时执行压缩过滤
        if (optimize_prompt) {
            msg = pre_filter_.PreFilterMessages(msg, contextSize, system_msg + developer_msg, /*is_harmony=*/true, &tool_call_id_to_name_);
        }

        std::vector<std::string> messages;
        messages.push_back(system_msg);
        if (!developer_msg.empty()) {
            messages.push_back(developer_msg);
        }

        // 添加历史消息
        std::vector<GenieChatMessage> all_messages;

        all_messages.push_back({"user", model_input_.text_});
        for (const auto &element: msg)
        {
            auto role = get_json_value(element, "role", BLANK_STRING);
            std::string content;

            if (role == "assistant")
            {
                // 检查是否有 tool_calls 字段（OpenAI 标准格式）
                if (element.contains("tool_calls") && !element["tool_calls"].is_null() &&
                    element["tool_calls"].is_array())
                {
                    const auto& tool_calls = element["tool_calls"];
                    size_t num_tool_calls = tool_calls.size();

                    // 多工具调用：生成前言（preamble）
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

                    // 转换 OpenAI 格式的工具调用为 Harmony 格式
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

                        // ========== JSON 参数验证 ==========
                        try {
                            if (!func_args.empty()) {
                                json args_json = json::parse(func_args);
                                My_Log{My_Log::Level::kDebug} << "[Tool Call] ✓ JSON validation passed for: " << func_name << std::endl;
                            } else {
                                // 空参数视为有效（无参数函数）
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

                            // 尝试修复：如果参数不是 JSON，包装为字符串
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

                        // 工具调用格式（Harmony 规范）：
                        // 1. Analysis 通道：CoT 思考
                        // 2. Commentary 通道：实际的工具调用

                        // 1. Analysis 通道
                        std::string analysis_msg = "<|start|>assistant<|channel|>analysis<|message|>";
                        analysis_msg += "Need to use function " + func_name + ".<|end|>";
                        all_messages.push_back({role, analysis_msg});

                        My_Log{My_Log::Level::kDebug} << "[Tool Call] ✓ Added analysis message for: " << func_name << std::endl;

                        // 2. Commentary 通道（工具调用）
                        // 格式要求："to=functions.xxx" 后面必须有空格，"<|constrain|>" 后紧跟 "json"
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
                        // 检测是否已经是有效的 Harmony 格式
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
                            // 转换为 Harmony 格式（final 通道）
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

                    // 策略 1：优先从 name 字段获取（OpenAI 标准字段）
                    if (element.contains("name") && element["name"].is_string())
                    {
                        tool_name = element["name"].get<std::string>();
                        My_Log{My_Log::Level::kDebug} << "[Tool Response] Using tool name from 'name' field: " << tool_name << std::endl;
                    }
                    // 策略 2：从 tool_call_id 映射获取（备用方案）
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

                    // 两种策略都失败时，使用默认工具名称（容错处理）
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

                    // 检查是否已经是 Harmony 格式
                    if (content.find("<|start|>functions.") != std::string::npos)
                    {
                        all_messages.push_back({role, content});
                        My_Log{My_Log::Level::kDebug} << "[Tool Response] Already in Harmony format" << std::endl;
                    }
                    else
                    {
                        // [SpawnGuard] 检测 sessions_spawn 异步响应，注入强制等待指令（Harmony 路径）
                        // 复用 PromptOptimizer::InjectSpawnGuard，与 General 格式路径共享同一实现
                        std::string content_for_harmony = optimizer_.InjectSpawnGuard(content);

                        // 转换为 Harmony 格式
                        std::string harmony_msg = HarmonyProcessor::BuildToolMessage(
                            tool_name,
                            content_for_harmony
                        );
                        all_messages.push_back({role, harmony_msg});
                        My_Log{My_Log::Level::kDebug} << "[Tool Response] ✓ Converted to Harmony format: functions." << tool_name << std::endl;
                    }
                }
            }
            // system 消息已经在 developer 中处理，跳过
        }

        // ========== CoT 管理逻辑 ==========
        // 根据 Harmony 规范：
        // 1. 工具调用场景：保留 analysis 通道（CoT）
        // 2. 一般对话场景：丢弃 analysis 通道，只保留 final 通道
        //
        // 单次正向扫描状态机（O(n)）识别所有工具调用序列的范围

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

        // 判断 assistant 消息是否是 analysis 消息
        auto is_analysis_msg = [](const std::string& content) -> bool {
            return content.find("<|channel|>analysis") != std::string::npos;
        };
        // 判断 assistant 消息是否是 preamble 消息
        // preamble：commentary 通道但不含 to=functions.（多工具调用前言）
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

        // 第二遍：根据范围应用 CoT 规则
        // 预构建 in_range / is_endpoint 标志数组（O(n)）
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
                // 工具响应：始终保留完整消息
                processed_messages.push_back(msg_info);
                My_Log{My_Log::Level::kDebug} << "[CoT] Tool response at index " << i << ": keeping full message" << std::endl;
            }
            else if (msg_info.role == "assistant") {
                if (in_tool_call_range) {
                    // 工具调用范围内的 assistant 消息：
                    // 1. 工具调用相关消息（analysis/commentary/preamble）：保留完整消息
                    // 2. 范围终点的处理工具结果消息（final 通道回复）：只保留 final 通道
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
                            // 如果没有 final 通道，保留原始消息（向后兼容）
                            processed_messages.push_back(msg_info);
                            My_Log{My_Log::Level::kDebug} << "[CoT] Tool-result assistant at range endpoint " << i
                                    << ": no final channel found, keeping original" << std::endl;
                        }
                    } else {
                        // 范围内的其他 assistant 消息（保留完整消息作为兜底）
                        processed_messages.push_back(msg_info);
                        My_Log{My_Log::Level::kDebug} << "[CoT] In-range assistant at index " << i
                                << ": keeping full message (fallback)" << std::endl;
                    }
                } else {
                    // 一般 assistant 响应：只保留 final 通道
                    std::string final_only = ExtractFinalChannel(msg_info.content);
                    if (!final_only.empty()) {
                        processed_messages.push_back({msg_info.role, final_only});
                        My_Log{My_Log::Level::kDebug} << "[CoT] Regular assistant at index " << i
                                << ": keeping only final channel" << std::endl;
                    } else {
                        // 如果没有 final 通道，保留原始消息（向后兼容）
                        processed_messages.push_back(msg_info);
                        My_Log{My_Log::Level::kDebug}
                            << "[CoT] Regular assistant at index " << i
                            << ": no final channel found, keeping original" << std::endl;
                    }
                }
            }
            else {
                // user 消息：直接添加
                processed_messages.push_back(msg_info);
            }
        }

        // 应用 FitMessagesToContext 进行 Token 控制
        std::string full_system_prompt = system_msg + developer_msg;

        OptimizedMessages optimized;
        chat_history_.Clear();
        if (optimize_prompt) {
            // 包装 user 消息为 Harmony 格式（用于 FitMessagesToContext 的 token 计算）
            // 保存原始内容，避免写入 chat_history_ 时依赖字符串解包
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

            // 写入 chat_history_ 时使用原始文本（非 Harmony 格式），
            // 避免 export_to_json 等外部接口读取到 Harmony 格式内容
            // 使用索引顺序匹配，不依赖内容相等性
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

            // 5. 构建当前请求的提示词（使用优化后的消息）
            for (const auto &msg_info : optimized.messages)
            {
                messages.push_back(msg_info.content);
            }
        } else {
            // n != -1：不压缩，直接写入历史并构建提示词
            for (const auto& msg_item : processed_messages) {
                chat_history_.AddMessage(msg_item.role, msg_item.content);
            }
            // 构建提示词（需要包装 user 消息为 Harmony 格式）
            for (const auto& msg_item : processed_messages) {
                if (msg_item.role == "user") {
                    messages.push_back(HarmonyProcessor::BuildUserMessage(msg_item.content));
                } else {
                    messages.push_back(msg_item.content);
                }
            }
            optimized.messages = processed_messages;
            optimized.success = true;
            optimized.total_tokens = 0;
        }

        // 6. 添加 assistant 起始标记
        messages.push_back("<|start|>assistant");

        // 7. 合并所有消息
        std::string result;
        for (const auto &harmony_part : messages) {
            result += harmony_part;
        }

        // 8. 计算最终提示词的真实 token 数
        // 修复：使用注入的 context_（多模型并发安全），而非 model_config_.get_genie_model_handle()
        // 在多模型场景下，get_genie_model_handle() 返回的是全局单模型句柄，会导致所有请求
        // 使用同一个模型句柄进行 TokenLength 计算，破坏多模型路由的正确性。
        if (optimize_prompt && instance_config_->getenablePromptDebug()) {
            // 包装 context_ 为非拥有 shared_ptr，传给需要 shared_ptr 参数的统计辅助函数

            // 打印详细的消息列表（PreFilter 后的原始 JSON 消息）
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

            // 统一打印所有统计信息
            // 1. 打印优化前统计信息
            if (context_) {
                PromptStatsHelper::PrintBeforeOptimizationStatsForHarmony(
                    before_opt_msg_snapshot, optimizer_, context_, contextSize,
                    knowledge_cutoff, current_date, reasoning_level, has_tools,
                    instructions, tools, instance_config_->get_context_size());
            }

            // 2. 打印 PreFilter 优化后统计信息
            size_t system_tokens = context_->TokenLength(system_msg + developer_msg);
            size_t tools_tokens = 0;
            if (tools.is_array() && !tools.empty()) {
                std::string tools_str = tools.dump();
                tools_tokens = context_->TokenLength(tools_str);
            }
            PromptStatsHelper::PrintAfterPreFilterStats(msg, context_, system_tokens, contextSize, tools_tokens, &pre_filter_.GetStats(),
                                                        instance_config_->get_context_size(), /*is_harmony=*/true);

            // 3. 打印 FitMessagesToContext 优化后统计信息
            ComputeAndPrintFitStats(optimized, context_, system_tokens, tools_tokens, contextSize);

            // 4. 打印最终提示词统计信息
            size_t final_tokens = context_->TokenLength(result);
            PromptStatsHelper::PrintFinalStats(final_tokens, result.length(), contextSize, has_tools,
                                               instance_config_->get_context_size());
        }
        return result;
    }

    // ========== 辅助函数：记录 Agent 类型检测结果日志 ==========
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

    // ========== 辅助函数：检测 Agent 类型并记录日志 ==========
    AgentType DetectAgentTypeAndLog(const std::string& system_prompt, const std::string& context_prefix)
    {
        AgentType agentType = optimizer_.DetectAgentType(system_prompt);
        LogAgentType(agentType, context_prefix);
        return agentType;
    }

    // ========== 辅助函数：调用 FitMessagesToContext 并统一处理错误和日志 ==========
    OptimizedMessages ApplyFitMessagesToContext(
        const std::vector<GenieChatMessage>& messages,
        const std::string& system_prompt,
        int contextSize,
        const std::string& log_prefix)
    {
        My_Log{My_Log::Level::kDebug} << "[" << log_prefix << "] Fitting messages to context..." << std::endl;

        auto optimized = pre_filter_.FitMessagesToContext(messages, system_prompt, contextSize);

        if (!optimized.success) {
            My_Log{My_Log::Level::kError}
                << "[" << log_prefix << "] Failed: " << optimized.error_message << std::endl;
            throw ReportError{std::move(optimized.error_message)};
        }

        My_Log{My_Log::Level::kInfo} << "[" << log_prefix << "] Success - Messages: "
                                      << optimized.messages.size()
                                      << ", Tokens: " << optimized.total_tokens << std::endl;
        return optimized;
    }

    // ========== 辅助方法：计算并打印 FitMessagesToContext 统计信息 ==========
    void ComputeAndPrintFitStats(
        const OptimizedMessages& optimized,
        std::shared_ptr<ContextBase> model_handle,
        size_t system_tokens,
        size_t tools_tokens,
        int contextSize)
    {
        // 修复：使用 instance_config_（per-model）而非 model_config_（全局 IModelConfig）
        PromptStatsHelper::PrintAfterFitStatsFromOptimized(
            optimized.messages, model_handle,
            system_tokens, tools_tokens,
            contextSize, instance_config_->get_context_size());
    }

    // 辅助函数 - 提取 final 通道内容
    // 用于 CoT 管理：一般对话只保留 final 通道
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

            // 重新构建为 final 通道消息
            result += "<|start|>assistant" + kFinalChannelMarker + content + kEndMarker;

            pos = content_end + end_marker_len;
        }

        return result;
    }

    // 辅助函数 - 检测是否是工具调用消息
    bool IsToolCallMessage(const std::string& harmony_msg) const
    {
        return harmony_msg.find("to=functions.") != std::string::npos;
    }

    // 获取当前日期
    std::string getCurrentDate() const
    {
        auto now = std::chrono::system_clock::now();
        auto time_t = std::chrono::system_clock::to_time_t(now);
        // 使用线程安全版本替代 std::localtime（std::localtime 在多线程下存在数据竞争）
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

    // ── RunSummarizationInference ─────────────────────────────────────────────
    // 为 Phase -1 摘要化执行单次同步推理。
    // 直接构造 ModelInput 并调用 context_->Query()，不走 Build() 主流程（避免递归）。
    // 推理失败或输出为空时返回空串，调用方保留原文。
    //
    // prefill_heartbeat：可选的 prefill 阶段心跳回调，在 prefill 期间定期调用。
    // 用于在摘要推理的 prefill 阶段向客户端发送保活帧，防止 read 超时。
    // 返回 false 表示连接已断开，应中止推理。
    std::string RunSummarizationInference(const std::string& prompt,
                                          ContextBase::PrefillHeartbeatCallback prefill_heartbeat = nullptr)
    {
        if (prompt.empty()) return "";

        ModelInput sum_input;
        sum_input.text_ = prompt;

        // 动态调整 max_length：根据 prompt token 数计算可用 output tokens，
        // 确保摘要推理有足够的输出空间，避免因 output token limit 导致摘要被截断。
        // 策略：context_size - prompt_tokens，至少保留 512 tokens 用于输出。
        {
            int context_size = instance_config_->get_context_size();
            size_t prompt_tokens = context_->TokenLength(prompt);
            int available_output = context_size - static_cast<int>(prompt_tokens);
            // 至少保留 512 tokens 输出，最多使用整个 context_size
            available_output = std::max(available_output, 512);
            available_output = std::min(available_output, context_size);
            context_->SetParams("temperature", "0.3");
            context_->SetParams("top_k", "20");
            context_->SetParams("top_p", "0.8");
        }

        std::string result;
        bool success = false;

        try
        {
            success = context_->Query(
                sum_input,
                [&result](std::string& token) -> bool {
                    // [调试] 流式打印每个 token 到日志（与主推理的 genie_callback 风格一致）
                    if (!token.empty())
                    {
                        My_Log{}.original(true) << token;
                    }
                    result += token;
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

        // ── 打印性能数据（与主推理格式一致）──────────────────────────────────
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

        if (!success)
        {
            My_Log{My_Log::Level::kWarning}
                << "[RunSummarizationInference] Query returned false, result so far ("
                << result.size() << " chars): "
                << (result.empty() ? "<empty>" : result.substr(0, std::min(result.size(), size_t(200))))
                << std::endl;
            return "";
        }

        // [调试] 打印完整的摘要推理输出，便于诊断摘要质量
        // 使用 kWarning 级别确保一定输出，用 std::endl 保证立即刷新到日志文件
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
        model_input_.agent_type_ = "sub";  // 默认为子 Agent，由 Build() 中检测后覆盖
        tool_call_id_to_name_.clear();
    }

    static inline const std::string FILL_THINK = "<think>\n\n</think>\n\n";

    static inline const std::string BLANK_STRING;

    // 注意：C++ 按声明顺序初始化成员变量，与初始化列表顺序无关。
    // 以下声明顺序与构造函数初始化列表顺序保持一致，消除 -Wreorder 编译器警告。
    // 初始化顺序：chat_history_ → instance_config_ → context_ → optimizer_ → pre_filter_
    // optimizer_ 和 pre_filter_ 依赖 instance_config_（已在前面初始化），
    // SetContext() 在构造函数体中调用（此时 context_ 已初始化），无 UB 风险。
    ChatHistory &chat_history_;
    ModelInput model_input_;
    ModelInstanceConfig *instance_config_;
    std::shared_ptr<ContextBase> context_;
    PromptOptimizer optimizer_;
    MessagePreFilter pre_filter_;

    // 工具调用 ID 到函数名的映射（用于关联 OpenAI 格式的工具调用和响应）
    std::unordered_map<std::string, std::string> tool_call_id_to_name_;

    // 保存请求数据，用于提取 Skill 描述
    // 注意：必须使用值拷贝（而非引用），因为在 stream 路径中 Build() 在 lambda 内异步执行，
    // 此时外部的 data 已超出作用域，若使用引用会导致悬空引用（dangling reference），
    // 使 ExtractSkillsFromRequest 无法从 request_data_ 中解析到 <available_skills> XML，
    // 导致本地模型路径的 Skill Catalog 丢失。
    json request_data_;
};

#endif //PROMPT_H
