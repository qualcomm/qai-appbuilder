//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
//
// genie_routing_gateway_cloud.cpp
//
// 职责：
//   - 云端请求执行（ExecuteCloudRequest）
//     - 流式 SSE 转发（含滑动窗口脱敏还原）
//     - 非流式请求转发（含响应还原）
//   - 辅助函数：FormatCloudRequestPrompt / ExtractDeltaContent /
//               ExtractTokenUsage / RestoreStringFully
//   - 非流式响应还原（RestoreCloudResponse）
//   - 策略错误响应（SendPolicyViolationError）
//   - 云端响应转发（HandleCloudResponse）
//
//==============================================================================

#include "gateway.h"
#include "log.h"
#include "../../response/response_dispatcher.h"
#include "../../response/response_tools.h"
#include <chrono>
#include <sstream>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <algorithm>
#include <map>
#include <limits>

// ============================================================
// 辅助函数：将 cloud_request 中的 messages 格式化为可读的 Prompt 字符串
// ============================================================
static std::string FormatCloudRequestPrompt(const json &cloud_request)
{
    std::ostringstream oss;
    try
    {
        if (cloud_request.contains("messages") && cloud_request["messages"].is_array())
        {
            for (const auto &msg : cloud_request["messages"])
            {
                std::string role = msg.value("role", "unknown");
                // content 字段存在且非 null 时才处理 content；
                // 若 content 为 null（OpenAI 标准：纯 tool_calls 消息的 content 字段为 null），
                // 则跳过 content 处理，转而检查 tool_calls 字段。
                bool has_non_null_content = msg.contains("content") && !msg["content"].is_null();
                if (has_non_null_content)
                {
                    if (msg["content"].is_string())
                    {
                        oss << "<|start|>" << role << "<|message|>" << msg["content"].get<std::string>() << "\n";
                    }
                    else if (msg["content"].is_array())
                    {
                        oss << "<|start|>" << role << "<|message|>";
                        for (const auto &part : msg["content"])
                        {
                            if (part.contains("type") && part["type"] == "text" && part.contains("text"))
                            {
                                oss << part["text"].get<std::string>();
                            }
                        }
                        oss << "\n";
                    }
                    else
                    {
                        oss << "<|start|>" << role << "<|message|>" << msg["content"].dump() << "\n";
                    }
                }

                if (msg.contains("tool_calls") && !msg["tool_calls"].is_null() && msg["tool_calls"].is_array())
                {
                    // assistant 消息含 tool_calls（content 可能为 null 或有文本）：打印完整工具调用 JSON
                    if (!has_non_null_content)
                    {
                        // 纯 tool_calls 消息（content 为 null 或不存在）：打印 role 头 + tool_calls JSON
                        oss << "<|start|>" << role << "<|message|>" << msg["tool_calls"].dump() << "\n";
                    }
                    else
                    {
                        // content + tool_calls 并存：在 content 行之后追加 tool_calls JSON
                        oss << "<|tool_calls|>" << msg["tool_calls"].dump() << "\n";
                    }
                }
                else if (!has_non_null_content && !msg.contains("tool_calls"))
                {
                    // 既没有非 null 的 content 也没有 tool_calls：打印 role 但标记为空消息
                    oss << "<|start|>" << role << "<|message|>(empty)\n";
                }
            }
        }
        else
        {
            oss << cloud_request.dump(2);
        }
    }
    catch (const std::exception &e)
    {
        oss << "(failed to format prompt: " << e.what() << ")";
    }
    return oss.str();
}

// ============================================================
// 辅助函数：从 SSE chunk JSON 字符串中提取 delta content 文本
// 同时提取 delta.tool_calls 中的函数名和参数片段（用于日志累积）
// ============================================================
static std::string ExtractDeltaContent(const std::string &chunk_json)
{
    try
    {
        auto j = json::parse(chunk_json);
        if (j.contains("choices") && j["choices"].is_array() && !j["choices"].empty())
        {
            const auto &choice = j["choices"][0];
            if (choice.contains("delta") && choice["delta"].is_object())
            {
                const auto &delta = choice["delta"];
                // 优先提取 content 文本
                if (delta.contains("content") && delta["content"].is_string())
                {
                    return delta["content"].get<std::string>();
                }
                // 若无 content，提取 tool_calls 中的函数名/参数片段（用于日志）
                if (delta.contains("tool_calls") && delta["tool_calls"].is_array())
                {
                    std::string tool_text;
                    for (const auto &tc : delta["tool_calls"])
                    {
                        if (tc.contains("function"))
                        {
                            const auto &func = tc["function"];
                            // 函数名（通常在第一个 chunk 中出现）
                            if (func.contains("name") && func["name"].is_string())
                            {
                                std::string name = func["name"].get<std::string>();
                                if (!name.empty())
                                    tool_text += "[tool_call:" + name + "]";
                            }
                            // 参数片段（流式分片，逐步累积）
                            if (func.contains("arguments") && func["arguments"].is_string())
                            {
                                tool_text += func["arguments"].get<std::string>();
                            }
                        }
                    }
                    return tool_text;
                }
            }
        }
    }
    catch (...)
    {
        // 忽略解析错误
    }
    return "";
}

// ============================================================
// Token 使用量详情结构（用于详细日志打印）
// ============================================================
struct TokenUsageDetail {
    int64_t prompt_tokens = 0;       // Prompt（输入）消耗的 token 数
    int64_t completion_tokens = 0;   // 模型生成（输出）消耗的 token 数
    int64_t total_tokens = 0;        // 总 token 数（prompt + completion）
    bool has_data = false;           // 是否有有效数据
};

// 辅助函数：从 JSON usage 对象中提取 Token 使用量详情
static TokenUsageDetail ExtractTokenUsageFromJson(const json &usage)
{
    TokenUsageDetail detail;
    detail.prompt_tokens = usage.value("prompt_tokens", (int64_t)0);
    detail.completion_tokens = usage.value("completion_tokens", (int64_t)0);
    if (usage.contains("total_tokens") && usage["total_tokens"].is_number())
    {
        detail.total_tokens = usage["total_tokens"].get<int64_t>();
    }
    else
    {
        detail.total_tokens = detail.prompt_tokens + detail.completion_tokens;
    }
    detail.has_data = (detail.total_tokens > 0 || detail.prompt_tokens > 0 || detail.completion_tokens > 0);
    return detail;
}

// 辅助函数：从 SSE chunk JSON 字符串中提取 usage 详情（流式最后一个 chunk 可能包含 usage）
// has_data=false 表示未找到 usage 字段
static TokenUsageDetail ExtractChunkTokenUsage(const std::string &chunk_json)
{
    TokenUsageDetail detail;
    try
    {
        auto j = json::parse(chunk_json);
        if (j.contains("usage") && j["usage"].is_object())
        {
            detail = ExtractTokenUsageFromJson(j["usage"]);
        }
    }
    catch (...)
    {
        // 忽略解析错误
    }
    return detail;
}

// 辅助函数：从非流式响应 JSON 中提取 usage 详情
// has_data=false 表示未找到 usage 字段
static TokenUsageDetail ExtractResponseTokenUsage(const json &response)
{
    TokenUsageDetail detail;
    try
    {
        if (response.contains("usage") && response["usage"].is_object())
        {
            detail = ExtractTokenUsageFromJson(response["usage"]);
        }
    }
    catch (...)
    {
        // 忽略解析错误
    }
    return detail;
}

// ============================================================
// RestoreStringFully：链式恢复字符串中的脱敏值，直到稳定或达到最大轮数
// ============================================================
std::string GenieRoutingGateway::RestoreStringFully(
        const std::string &text,
        const std::unordered_map<std::string, std::string> &mapping) const
{
    if (text.empty() || mapping.empty()) {
        return text;
    }

    std::string current = text;

    // 优先替换更长的 mock key，避免短 key 先命中导致长 key 被拆断。
    // 典型场景：mock_user_6@example.com 与 mock_user_5@example.com 共享前缀，
    // 按长度降序替换更稳妥，也能避免局部还原后影响后续完整匹配。
    std::vector<std::pair<std::string, std::string>> ordered_mapping(
        mapping.begin(), mapping.end());
    std::sort(ordered_mapping.begin(), ordered_mapping.end(),
              [](const auto &a, const auto &b) {
                  if (a.first.size() != b.first.size()) {
                      return a.first.size() > b.first.size();
                  }
                  return a.first < b.first;
              });

    // mapping key 现在只存储不含特殊字符的简单标识符
    // （如 mock_user_N、mock_user_N@example.com 等），不再含有路径分隔符（/ \）
    // 或需要 JSON 转义的字符（\），因此只需直接字符串搜索替换即可，
    // 无需路径分隔符标准化或 JSON 转义处理。
    // max_rounds 防止异常映射（如 A→B, B→A 循环）导致死循环。
    const size_t max_rounds = mapping.size() + 2;

    for (size_t round = 0; round < max_rounds; ++round)
    {
        bool changed = false;
        for (const auto &[mock, real] : ordered_mapping)
        {
            size_t pos = 0;
            while ((pos = current.find(mock, pos)) != std::string::npos)
            {
                current.replace(pos, mock.size(), real);
                pos += real.size();
                changed = true;
            }
        }

        if (!changed) {
            break;
        }
    }

    return current;
}

// ============================================================
// SendPolicyViolationError：生成策略拒绝/服务不可用错误响应
// ============================================================
void GenieRoutingGateway::SendPolicyViolationError(httplib::Response &res,
                                           const std::string &code,
                                           const std::string &message,
                                           int http_status,
                                           bool is_stream)
{
    // 对应设计文档 §10.3 错误码字典的标准错误响应
    // http_status=403 → type="policy_violation_error"（策略拒绝）
    // http_status=503 → type="service_unavailable_error"（服务不可用）
    std::string type = (http_status == 503) ? "service_unavailable_error" : "policy_violation_error";

    json error_obj;
    error_obj["message"] = message;
    error_obj["type"] = type;
    error_obj["code"] = code;
    error_obj["param"] = json(nullptr);

    json error_response;
    error_response["error"] = error_obj;

    if (is_stream)
    {
        // 流式请求场景：以 SSE 格式发送错误响应
        // 客户端（如 OpenClaw）期望流式请求的响应是 SSE 格式，
        // 若以普通 JSON 格式返回，客户端可能无法正确解析并显示错误信息。
        // SSE 格式：data: <JSON>\n\ndata: [DONE]\n\n
        std::string sse_body = "data: " + error_response.dump() + "\n\ndata: [DONE]\n\n";
        res.status = 200;  // SSE 流式响应通常以 200 开始，错误信息在 body 中
        res.set_content(sse_body, "text/event-stream");
        My_Log{} << "[GenieRoutingGateway] Sent SSE error response (code=" << code
                 << ", http_status=" << http_status << ")" << std::endl;
    }
    else
    {
        res.status = http_status;
        res.set_content(error_response.dump(), ResponseDispatcher::MIMETYPE_JSON);

        // 对于 503/502 错误，添加 Retry-After 响应头，告知客户端建议的重试等待时间。
        // 这样可以避免客户端立即无限重试，减少服务器压力。
        // 503: 云端不可用，60秒后恢复（对应 CloudModelClient::RECOVERY_INTERVAL_SECONDS）
        // 403: 策略拒绝，不建议重试（不添加 Retry-After）
        if (http_status == 503)
        {
            res.set_header("Retry-After", "60");
            My_Log{} << "[GenieRoutingGateway] Added Retry-After: 60 to 503 response (code=" << code << ")" << std::endl;
        }
    }
}

// ============================================================
// HandleCloudResponse：将云端响应直接转发给客户端
// ============================================================
bool GenieRoutingGateway::HandleCloudResponse(const json &cloud_response,
                                      httplib::Response &http_res)
{
    // 将云端响应直接转发给客户端
    // 云端响应应该已经是标准的 OpenAI 格式
    try
    {
        http_res.status = 200;
        http_res.set_content(cloud_response.dump(), ResponseDispatcher::MIMETYPE_JSON);
        return true;
    }
    catch (const std::exception &e)
    {
        My_Log{My_Log::Level::kError} << "[GenieRoutingGateway] Failed to serialize cloud response: "
                                      << e.what() << std::endl;
        return false;
    }
}

// ============================================================
// RestoreCloudResponse：非流式响应还原（将 Mock Data 替换回原始数据）
// ============================================================
void GenieRoutingGateway::RestoreCloudResponse(
        json &response,
        const std::unordered_map<std::string, std::string> &mapping) const
{
    if (mapping.empty()) {
        return;
    }

    if (!response.contains("choices") || !response["choices"].is_array()) {
        return;
    }

    // 辅助函数：对字符串执行映射替换
    auto restore_string = [this, &mapping](std::string &text) {
        text = RestoreStringFully(text, mapping);
    };

    // 递归深度保护：防止恶意构造的深层嵌套 JSON 导致栈溢出
    // 最大递归深度设为 50 层（正常响应通常不超过 10 层）
    constexpr int MAX_RECURSION_DEPTH = 50;

    // 递归遍历 JSON 并还原字符串字段
    std::function<void(json&, int)> restore_json_recursive = [&](json &node, int depth) {
        // 深度保护：超过最大深度时停止递归并记录警告
        if (depth > MAX_RECURSION_DEPTH) {
            My_Log{My_Log::Level::kWarning}
                << "[GenieRoutingGateway] RestoreCloudResponse: max recursion depth ("
                << MAX_RECURSION_DEPTH << ") exceeded, stopping recursion to prevent stack overflow"
                << std::endl;
            return;
        }

        if (node.is_string()) {
            std::string text = node.get<std::string>();
            restore_string(text);
            node = text;
        } else if (node.is_object()) {
            for (auto &[key, value] : node.items()) {
                restore_json_recursive(value, depth + 1);
            }
        } else if (node.is_array()) {
            for (auto &item : node) {
                restore_json_recursive(item, depth + 1);
            }
        }
    };

    // 遍历所有 choice
    for (auto &choice : response["choices"]) {
        if (!choice.contains("message")) {
            continue;
        }

        auto &message = choice["message"];

        // 1. 还原 content 字段
        if (message.contains("content") && message["content"].is_string()) {
            std::string content = message["content"].get<std::string>();
            restore_string(content);
            message["content"] = content;
        }

        // 2. 还原 tool_calls 中的 arguments（使用递归遍历，支持嵌套结构）
        if (message.contains("tool_calls") && message["tool_calls"].is_array()) {
            for (auto &tool_call : message["tool_calls"]) {
                if (!tool_call.contains("function")) {
                    continue;
                }

                auto &func = tool_call["function"];
                if (func.contains("arguments")) {
                    // arguments 可能是字符串或 JSON 对象，统一使用递归处理
                    restore_json_recursive(func["arguments"], 0);
                }
            }
        }
    }

    My_Log{}
        << "[GenieRoutingGateway] RestoreCloudResponse: restored "
        << mapping.size() << " mappings"
        << std::endl;
}

// ============================================================
// ExecuteCloudRequest：执行云端请求（流式 + 非流式）
// ============================================================
bool GenieRoutingGateway::ExecuteCloudRequest(const json &cloud_request,
                                              bool is_stream,
                                              httplib::Response &http_res,
                                              bool &handled_by_cloud,
                                              const std::string &user_session_id,
                                              httplib::DataSink *sink,
                                              const std::string &initial_status,
                                              const std::string &initial_message,
                                              CloudTier cloud_tier)
{
    std::string error_msg;

    // 根据 cloud_tier 选择对应的云端客户端
    // ENTERPRISE → enterprise_cloud_client_（企业内网云）
    // PUBLIC     → cloud_client_（外部公有云，默认）
    CloudModelClient &active_client = (cloud_tier == CloudTier::ENTERPRISE)
        ? enterprise_cloud_client_
        : cloud_client_;
    const char *tier_label = (cloud_tier == CloudTier::ENTERPRISE) ? "Enterprise" : "Public";

    // ── 云端推理限流检查（推理次数 + Token 数）──────────────────────────────
    // 在执行云端请求前检查是否已超限
    // 使用服务端生成的 user_session_id（不再从 cloud_request 读取，客户端不传 session_id）
    const std::string &session_id = user_session_id;
    if (!CheckAndIncrementInferenceCount(session_id, http_res, cloud_tier))
    {
        // 已超限，http_res 已写入 429 错误响应
        // 返回 true + handled_by_cloud=true：告知调用方请求已被处理（429 错误响应），
        // 避免调用方恢复本地响应或继续本地流程
        handled_by_cloud = true;
        return true;
    }

    // 打印发送给云端模型的 Prompt 日志
    // 从 cloud_request 中检测 agent 类型
    // 检测优先级：
    //   1. system prompt 含 "agent=main"（原始 QAIAgentForge/OpenClaw 格式）
    //   2. system prompt 含 "CRITICAL RULE"（PromptPreparationService 优化后的格式，
    //      BuildSystemContext 输出的 system prompt 以 CRITICAL RULE 开头，表示 main agent）
    //   3. 以上均不含 → SUBAGENT_INFERENCE
    {
        std::string cloud_system_prompt;
        if (cloud_request.contains("messages") && cloud_request["messages"].is_array()) {
            for (const auto& msg : cloud_request["messages"]) {
                if (msg.is_object() && msg.value("role", "") == "system") {
                    cloud_system_prompt = msg.value("content", "");
                    break;
                }
            }
        }
        bool is_main_agent = (cloud_system_prompt.find("agent=main") != std::string::npos) ||
                             (cloud_system_prompt.find("CRITICAL RULE") != std::string::npos);
        const char *cloud_label = is_main_agent ? "MAINAGENT_INFERENCE" : "SUBAGENT_INFERENCE";
        My_Log{} << "\n[Prompt] [" << cloud_label << "][" << tier_label << "Cloud]:\n"
                  << FormatCloudRequestPrompt(cloud_request)
                  << "------------\n\n"
                  << "[Response] [" << cloud_label << "][" << tier_label << "Cloud]:\n";

        // 打印发送给云端的完整 JSON body（含 messages + tools），用于调试验证格式
        // 注意：此处打印的是 model 字段替换前的内容，替换后的内容见下方 mutable_request
        // 默认关闭（避免高频完整请求体刷屏），仅当对应云端层级开启 log_debug 时打印
        bool cloud_log_debug = (cloud_tier == CloudTier::ENTERPRISE)
            ? enterprise_cloud_config_.log_debug
            : cloud_config_.log_debug;
        if (cloud_log_debug) {
            My_Log{} << "[CloudRequest][JSON] " << cloud_request.dump() << std::endl;
        }
    }

    // ── 清理并规范化发送给云端的请求 ─────────────────────────────────────────
    // 1. 替换 model 字段为云端配置的模型名
    //    客户端（如 QAIAgentForge）发来的 model 字段是本地模型名（如 qwen3-8b-8380），
    //    云端服务不认识本地模型名，必须替换为 service_config.json 中配置的云端模型名。
    // 2. 移除非标准字段（session_id 等 GenieAPIService 内部字段）
    //    这些字段不是 OpenAI API 标准字段，部分云端服务（如 qgenie-chat.qualcomm.com）
    //    会因为收到未知字段而返回 HTTP 415 Unsupported Media Type。
    // 3. 移除 max_tokens / max_completion_tokens 字段
    //    客户端（如 OpenClaw）可能传入过大的值（如 32000），加上输入 token 数后超出
    //    云端模型的上下文窗口（如 32768），导致 vLLM 返回 HTTP 400。
    //    移除后由云端服务使用默认值，避免超限。
    json mutable_request = cloud_request;
    {
        // 替换 model 字段
        const std::string &cloud_model_name = (cloud_tier == CloudTier::ENTERPRISE)
            ? enterprise_cloud_config_.model
            : cloud_config_.model;
        if (!cloud_model_name.empty())
        {
            mutable_request["model"] = cloud_model_name;
            My_Log{} << "[GenieRoutingGateway] Overriding model field: '"
                     << cloud_request.value("model", "(not set)")
                     << "' -> '" << cloud_model_name << "'" << std::endl;
        }

        // 移除 max_tokens / max_completion_tokens：
        // 客户端传入的值可能超出云端模型上下文窗口（input_tokens + max_tokens > context_size），
        // 导致 vLLM 返回 HTTP 400 BadRequestError。移除后由云端服务自行决定输出长度上限。
        if (mutable_request.contains("max_tokens"))
        {
            My_Log{} << "[GenieRoutingGateway] Removing 'max_tokens' field (value="
                     << mutable_request["max_tokens"].dump()
                     << ") to avoid exceeding cloud model context window" << std::endl;
            mutable_request.erase("max_tokens");
        }
        if (mutable_request.contains("max_completion_tokens"))
        {
            My_Log{} << "[GenieRoutingGateway] Removing 'max_completion_tokens' field (value="
                     << mutable_request["max_completion_tokens"].dump()
                     << ") to avoid exceeding cloud model context window" << std::endl;
            mutable_request.erase("max_completion_tokens");
        }
    }

    if (is_stream)
    {
        My_Log{} << "[GenieRoutingGateway] " << tier_label << " cloud route with SSE streaming (true streaming)" << std::endl;

        struct StreamState {
            std::mutex mtx;
            std::condition_variable cv;
            std::vector<std::string> chunks;
            bool done = false;                  // 生产者已完成（成功或失败）
            bool success = false;               // 生产者是否成功
            bool consumer_stopped = false;      // 消费者已停止（客户端断开或发生错误）
            std::string error_msg;
            std::string accumulated_response;   // 累积的响应文本（用于日志打印）
            // Token 使用量（从最后一个 chunk 中提取，通常在流式响应的最后一个 chunk 中）
            int64_t total_tokens = -1;          // 总 token 数（-1 表示未获取）
            int64_t prompt_tokens = 0;          // Prompt（输入）token 数
            int64_t completion_tokens = 0;      // 生成（输出）token 数
            bool has_token_data = false;        // 是否获取到了 token 使用量数据
        };
        auto state = std::make_shared<StreamState>();

        // 使用 active_client 指针（而非固定的 cloud_client_）
        CloudModelClient *cloud_client_ptr = &active_client;
        std::shared_ptr<std::atomic<bool>> alive_flag = alive_;
        std::thread producer([cloud_client_ptr, alive_flag, mutable_request, state]() mutable {
            if (!alive_flag->load())
            {
                std::lock_guard<std::mutex> lock(state->mtx);
                state->success = false;
                state->error_msg = "GenieRoutingGateway was destroyed before stream could start";
                state->done = true;
                state->cv.notify_all();
                return;
            }

            std::string err;
            bool ok = cloud_client_ptr->ChatCompletionStream(
                mutable_request,
                [&state](const std::string &chunk) -> bool {
                    {
                        std::lock_guard<std::mutex> lock(state->mtx);
                        // 若消费者已停止（客户端断开），通知生产者停止接收云端数据
                        if (state->consumer_stopped)
                        {
                            return false;  // 中止云端 SSE 接收
                        }
                        state->chunks.push_back(chunk);

                        // 累积响应文本（用于日志打印）
                        std::string delta_text = ExtractDeltaContent(chunk);
                        if (!delta_text.empty()) {
                            state->accumulated_response += delta_text;
                        }

                        // 尝试从每个 chunk 中提取 token 使用量（通常在最后一个 chunk 中）
                        auto usage = ExtractChunkTokenUsage(chunk);
                        if (usage.has_data)
                        {
                            state->total_tokens = usage.total_tokens;
                            state->prompt_tokens = usage.prompt_tokens;
                            state->completion_tokens = usage.completion_tokens;
                            state->has_token_data = true;
                        }
                    }
                    state->cv.notify_one();
                    return true;
                },
                err);

            {
                std::lock_guard<std::mutex> lock(state->mtx);
                state->success = ok;
                state->error_msg = err;
                state->done = true;
            }
            state->cv.notify_all();
        });
        producer.detach();

        // 等待第一个 chunk 或完成信号（最多等待 30s，与云端连接超时对齐）
        {
            std::unique_lock<std::mutex> lock(state->mtx);
            state->cv.wait_for(lock,
                std::chrono::seconds(30),
                [&state]{ return !state->chunks.empty() || state->done; });

            // 若已完成且失败（且没有任何 chunk 可以先发给客户端）
            if (state->done && !state->success && state->chunks.empty())
            {
                My_Log{My_Log::Level::kError} << "[GenieRoutingGateway] Cloud SSE stream failed early: "
                                              << state->error_msg << std::endl;
                if (routing_config_.fallback.cloud_unavailable_to_local)
                {
                    My_Log{} << "[GenieRoutingGateway] Falling back to local processing" << std::endl;
                    handled_by_cloud = false;
                    return true;
                }
                else
                {
                    json err_resp = {
                        {"error", {
                            {"message", "Cloud SSE stream failed: " + state->error_msg},
                            {"type", "cloud_error"},
                            {"code", "cloud_stream_failed"}
                        }}
                    };
                    http_res.status = 502;
                    http_res.set_content(err_resp.dump(), ResponseDispatcher::MIMETYPE_JSON);
                    // 502 错误：云端连接失败，建议客户端稍后重试
                    http_res.set_header("Retry-After", "30");
                    return false;
                }
            }
        }

        // 提取 provider lambda，支持两种执行模式：
        //   sink==nullptr：通过 set_chunked_content_provider 异步发起新流（正常路由路径）
        //   sink!=nullptr：同步执行，直接向现有 sink 写入（流式溢出回退路径）
        auto status_sent = std::make_shared<bool>(false);
        // 获取映射表用于流式还原（仅当配置启用时）
        // restore_stream_enabled 开关：允许临时禁用流式还原做 A/B 验证
        bool enable_stream_restore = routing_config_.desensitization.restore_response_enabled &&
                                     routing_config_.desensitization.restore_stream_enabled;
        auto mapping = enable_stream_restore
                        ? GetDesensitizationMapping(session_id)
                        : std::unordered_map<std::string, std::string>{};

        // 已知问题：此处打印 mock 值→真实敏感信息的映射表，缺少开关保护，用户已确认暂不修复（只记录不修复）。
        // 打印映射表状态（关键：确认是否启用滑动窗口）
        My_Log{} << "[GenieRoutingGateway] [RESTORE-CONFIG] session_id=" << session_id
                 << ", enable_stream_restore=" << enable_stream_restore
                 << ", mapping.size=" << mapping.size()
                 << std::endl;
        if (!mapping.empty() && mapping.size() <= 5) {
            // 打印映射表内容（仅当条目较少时）
            for (const auto &[mock, real] : mapping) {
                My_Log{} << "[GenieRoutingGateway] [RESTORE-CONFIG] mapping: \"" << mock
                         << "\" -> \"" << real << "\"" << std::endl;
            }
        }

        // 计算最长 mock key 长度（用于确定窗口尾部保留大小）
        // mock key 只存储不含特殊字符的简单标识符（如 mock_user_N），
        // 不含路径分隔符或反斜杠，无需额外计算 JSON 转义长度。
        size_t max_mock_len = 0;
        if (enable_stream_restore && !mapping.empty()) {
            for (const auto &[mock, real] : mapping) {
                max_mock_len = std::max(max_mock_len, mock.size());
            }
        }

        // 字段级缓冲区状态（跨 chunk 维护）
        struct StreamRestoreState {
            std::string content_buffer;  // delta.content 累积缓冲区
            std::map<int, std::pair<std::string, int>> args_buffers;  // tool_calls[index] -> (arguments_buffer, choice_index)
            int content_choice_index = 0;  // content 所属的 choice index（用于 flush 时构建正确的 chunk）
        };
        auto restore_state = std::make_shared<StreamRestoreState>();

        // 捕获 initial_status / initial_message，在第一次 provider 调用时发送给客户端
        auto provider = [this, state, session_id, status_sent, initial_status, initial_message, mapping, enable_stream_restore, max_mock_len, restore_state, cloud_tier](size_t /*offset*/, httplib::DataSink &s) -> bool {
          try {
            // 在第一次调用时发送初始状态，告知客户端云端模型正在工作
            if (!*status_sent) {
                *status_sent = true;
                ResponseTools::post_stream_data(s, "data",
                    ResponseTools::statusDataJson(initial_status, initial_message));
            }

            std::unique_lock<std::mutex> lock(state->mtx);

            // 等待新 chunk 或生产者完成（最多等待 60s，防止长时间无数据时挂起）
            state->cv.wait_for(lock,
                std::chrono::seconds(60),
                [&state]{ return !state->chunks.empty() || state->done; });

                // 消费队列中所有待发送的 chunk
            while (!state->chunks.empty())
            {
                std::string chunk = std::move(state->chunks.front());
                state->chunks.erase(state->chunks.begin());
                lock.unlock();

                // 字段级还原 chunk（处理跨 chunk 分割的 mock key）
                // 使用局部 lambda 计算 restored_chunk，以 return 代替原有的 goto send_chunk，
                // 使控制流更清晰，同时保持完全相同的功能语义。
                //
                // flush_write_failed：内层 lambda 返回类型为 std::string，
                // 无法直接 return false（bool → std::string 无隐式转换，编译错误）。
                // 改用 flag 变量：lambda 内写入 flag，lambda 返回后在外层 provider lambda
                // （返回类型为 bool）中检查并执行 return false。
                bool flush_write_failed = false;
                std::string restored_chunk = [&]() -> std::string {
                if (enable_stream_restore && !mapping.empty() && max_mock_len > 0) {
                    // 解析 chunk JSON
                    json chunk_json;
                    try {
                        chunk_json = json::parse(chunk);
                    } catch (const json::exception &e) {
                        // 解析失败：直接发送原始 chunk（如 [DONE]）
                        return chunk;
                    }

                    // 检查是否包含 choices 数组
                    if (!chunk_json.contains("choices") || !chunk_json["choices"].is_array()) {
                        // 不包含 choices：直接发送原始 chunk（如 usage chunk）
                        return chunk;
                    }

                    // 检查是否包含 finish_reason: "tool_calls"
                    // 若包含，必须先 flush 所有 args_buffers，再发送此 chunk
                    bool has_tool_calls_finish = false;
                    for (const auto &choice : chunk_json["choices"]) {
                        if (choice.contains("finish_reason") &&
                            choice["finish_reason"].is_string() &&
                            choice["finish_reason"].get<std::string>() == "tool_calls") {
                            has_tool_calls_finish = true;
                            break;
                        }
                    }

                    if (has_tool_calls_finish && enable_stream_restore && !mapping.empty()) {
                        // 在 flush 前，先把当前 finish chunk 自身携带的
                        // args 片段合并进缓冲区。否则 finish chunk 的最后一段 args
                        // （如 "scripts\\get_weather.py..."）在 flush 之后才被处理，
                        // 导致 buffer 在 flush 时只有截断的路径，RestoreStringFully 找不到
                        // 完整的 json_mock，mock_user_N 无法被还原为真实用户名。
                        for (auto &choice : chunk_json["choices"]) {
                            if (!choice.contains("delta") || !choice["delta"].is_object()) continue;
                            auto &delta_pre = choice["delta"];
                            if (!delta_pre.contains("tool_calls") || !delta_pre["tool_calls"].is_array()) continue;
                            for (auto &tc_pre : delta_pre["tool_calls"]) {
                                if (!tc_pre.contains("index") || !tc_pre["index"].is_number_integer()) continue;
                                int tc_idx = tc_pre["index"].get<int>();
                                if (!tc_pre.contains("function") || !tc_pre["function"].is_object()) continue;
                                auto &func_pre = tc_pre["function"];
                                if (func_pre.contains("arguments") && func_pre["arguments"].is_string()) {
                                    const std::string &frag = func_pre["arguments"].get<std::string>();
                                    if (!frag.empty()) {
                                        restore_state->args_buffers[tc_idx].first += frag;
                                        func_pre["arguments"] = "";  // 标记为已处理，避免后续正常流程重复累加
                                    }
                                }
                            }
                        }

                        // 先 flush 所有 args_buffers（确保参数完整）
                        My_Log{} << "[GenieRoutingGateway] [ToolCallFlush] Detected finish_reason=tool_calls, "
                                 << "args_buffers.size=" << restore_state->args_buffers.size()
                                 << ", flushing all buffers before sending finish chunk" << std::endl;

                        for (auto &[index, buffer_pair] : restore_state->args_buffers) {
                            if (buffer_pair.first.empty()) {
                                continue;
                            }

                            // 对剩余 arguments 执行还原
                            std::string remaining_args = buffer_pair.first;
                            std::string before_restore = remaining_args;  // 保存还原前的内容用于对比
                            remaining_args = RestoreStringFully(remaining_args, mapping);

                            // 记录 flush 时 args_buffer 中剩余内容（还原前），以及还原结果。
                            // 仅当 log_desensitization_details=true 时打印（含原始敏感信息，生产环境默认关闭）
                            if (routing_config_.desensitization.log_desensitization_details) {
                                My_Log{} << "[GenieRoutingGateway] [FlushBefore] args_buffer[" << index
                                         << "], buffered_length=" << before_restore.size()
                                         << ", FULL_CONTENT=\""
                                         << (before_restore.size() > 100 ? before_restore.substr(0, 100) + "..." : before_restore)
                                         << "\"" << std::endl;
                                if (before_restore != remaining_args) {
                                    My_Log{} << "[GenieRoutingGateway] [FlushAfter] Restoration applied to flushed args:" << std::endl;
                                    My_Log{} << "  BEFORE: \"" << before_restore << "\"" << std::endl;
                                    My_Log{} << "  AFTER:  \"" << remaining_args << "\"" << std::endl;
                                } else {
                                    My_Log{} << "[GenieRoutingGateway] [FlushAfter] No restoration needed (no mock keys found)" << std::endl;
                                }
                            }

                            // 构建并发送 flush chunk
                            json flush_chunk = {
                                {"choices", json::array({
                                    {
                                        {"delta", {
                                            {"tool_calls", json::array({
                                                {
                                                    {"index", index},
                                                    {"function", {{"arguments", remaining_args}}}
                                                }
                                            })}
                                        }},
                                        {"index", buffer_pair.second},
                                        {"finish_reason", json(nullptr)}
                                    }
                                })}
                            };
                            std::string sse_line = "data: " + flush_chunk.dump() + "\n\n";
                            if (!s.write(sse_line.c_str(), sse_line.size())) {
                                My_Log{} << "[GenieRoutingGateway] Consumer stopped during args flush" << std::endl;
                                {
                                    std::lock_guard<std::mutex> notify_lock(state->mtx);
                                    state->consumer_stopped = true;
                                    state->cv.notify_all();
                                }
                                // 内层 lambda 返回类型为 std::string，不能 return false。
                                // 设置 flag，由外层 provider lambda（返回 bool）在 lambda 调用后检查。
                                flush_write_failed = true;
                                return std::string{};
                            }

                            My_Log{} << "[GenieRoutingGateway] [ToolCallFlush] Successfully flushed args_buffer[" << index << "], "
                                     << "restored_length=" << remaining_args.size() << std::endl;

                            buffer_pair.first.clear();
                        }
                        restore_state->args_buffers.clear();

                        My_Log{} << "[GenieRoutingGateway] [ToolCallFlush] All args_buffers flushed successfully, "
                                 << "now sending finish_reason=tool_calls chunk" << std::endl;
                    }

                    // 处理每个 choice 的 delta.content 和 delta.tool_calls[].function.arguments
                    bool has_content_to_send = false;
                    for (auto &choice : chunk_json["choices"]) {
                        if (!choice.contains("delta") || !choice["delta"].is_object()) {
                            continue;
                        }

                        auto &delta = choice["delta"];

                        // 1. 处理 delta.content（流式文本内容）
                        if (delta.contains("content") && delta["content"].is_string()) {
                            if (enable_stream_restore && max_mock_len > 0) {
                                // 提取 delta.content 并追加到缓冲区
                                std::string delta_content = delta["content"].get<std::string>();
                                restore_state->content_buffer += delta_content;

                                // 记录 content 所属的 choice index（用于 flush 时构建正确的 chunk）
                                if (choice.contains("index") && choice["index"].is_number_integer()) {
                                    restore_state->content_choice_index = choice["index"].get<int>();
                                }

                                size_t buffer_size = restore_state->content_buffer.size();
                                // 计算安全发送长度（保留窗口尾部 max_mock_len - 1 字节）
                                // content 使用严格大于 `>`，确保 buffer_size == max_mock_len-1
                                // 时不进入此分支（safe_len 会为 0，无意义），与 args 逻辑保持一致。
                                if (buffer_size > max_mock_len - 1) {
                                    size_t safe_len = buffer_size - (max_mock_len - 1);

                                    // safe_len 最小值保护：
                                    // 经过 prefix 回退和 UTF-8 对齐后 safe_len 可能降为 0，
                                    // 但即使 safe_len > 0，若其值极小（如 2 字节），滑动窗口
                                    // 余量不足以覆盖最短 mock key，存在截断泄露风险。
                                    // 强制 safe_len 不低于 MIN_SAFE_LEN，确保足够的安全余量。
                                    // MIN_SAFE_LEN 设为 max_mock_len / 2，兼顾性能与安全。
                                    const size_t MIN_SAFE_LEN = max_mock_len / 2;

                                    // 避免把 mock key 的前缀提前发送出去。
                                    // 仅保留 max_mock_len-1 还不够：如果 safe_content 末尾正好是
                                    // "mock_user_4@example.c" 这样的前缀，后续 chunk 到来时就再也
                                    // 无法恢复成真实邮箱了。这里回退 safe_len，直到尾部不再是任何
                                    // mock key 的前缀，保证占位符只能以"完整串"形式进入 Restore。
                                    bool adjusted_for_prefix = false;
                                    while (safe_len > 0) {
                                        std::string candidate = restore_state->content_buffer.substr(0, safe_len);
                                        bool ends_with_mock_prefix = false;
                                        for (const auto &[mock, real] : mapping) {
                                            size_t max_prefix = std::min(mock.size() - 1, candidate.size());
                                            for (size_t prefix_len = max_prefix; prefix_len > 0; --prefix_len) {
                                                if (candidate.size() >= prefix_len &&
                                                    candidate.compare(candidate.size() - prefix_len, prefix_len,
                                                                      mock, 0, prefix_len) == 0) {
                                                    ends_with_mock_prefix = true;
                                                    adjusted_for_prefix = true;
                                                    --safe_len;
                                                    break;
                                                }
                                            }
                                            if (ends_with_mock_prefix) {
                                                break;
                                            }
                                        }
                                        if (!ends_with_mock_prefix) {
                                            break;
                                        }
                                    }

                                    // 应用 MIN_SAFE_LEN 下限：prefix 回退后 safe_len 可能极小，
                                    // 若小于 MIN_SAFE_LEN 则不发送（等待更多内容累积），避免窗口余量不足。
                                    if (safe_len < MIN_SAFE_LEN) {
                                        delta.erase("content");
                                        continue;  // 跳到 for (auto &choice : chunk_json["choices"]) 的下一次迭代
                                    }

                                    // 将截断点调整到 UTF-8 字符边界，避免截断多字节字符
                                    // UTF-8 续字节的特征：0x80 <= byte <= 0xBF（即最高两位为 10）
                                    // 向前回退，直到找到非续字节（字符起始字节）
                                    while (safe_len > 0 &&
                                           (static_cast<unsigned char>(restore_state->content_buffer[safe_len]) & 0xC0) == 0x80) {
                                        --safe_len;
                                    }
                                    if (safe_len == 0) {
                                        // 整个安全区域都是续字节（极端情况），跳过发送此 choice 的 content
                                        delta.erase("content");
                                        continue;  // 跳到 for (auto &choice : chunk_json["choices"]) 的下一次迭代
                                    }
                                    std::string safe_content = restore_state->content_buffer.substr(0, safe_len);
                                    std::string before_restore = safe_content;
                                    safe_content = RestoreStringFully(safe_content, mapping);
                                    bool did_restore = (safe_content != before_restore);
                                    if (did_restore || adjusted_for_prefix) {
                                        My_Log{} << "[GenieRoutingGateway] [ContentRestore] Restored content in sliding window, safe_len="
                                                 << safe_len << ", preview=\""
                                                 << (safe_content.size() > 80 ? safe_content.substr(0, 80) + "..." : safe_content)
                                                 << "\"" << std::endl;
                                    }

                                    // 更新 delta.content 为还原后的安全部分
                                    delta["content"] = safe_content;
                                    has_content_to_send = true;

                                    // 保留窗口尾部
                                    restore_state->content_buffer = restore_state->content_buffer.substr(safe_len);
                                } else {
                                    // 缓冲区不足：跳过发送此 chunk 的 content（等待累积更多内容）
                                    delta.erase("content");
                                }
                            } else {
                                has_content_to_send = true;
                            }
                        }

                        // 2. 处理 delta.tool_calls[].function.arguments（工具调用参数，核心场景）
                        if (delta.contains("tool_calls") && delta["tool_calls"].is_array()) {
                            for (auto &tool_call : delta["tool_calls"]) {
                                if (!tool_call.contains("index") || !tool_call["index"].is_number_integer()) {
                                    continue;
                                }
                                int index = tool_call["index"].get<int>();

                                if (!tool_call.contains("function") || !tool_call["function"].is_object()) {
                                    continue;
                                }

                                auto &func = tool_call["function"];

                                if (func.contains("arguments") && func["arguments"].is_string()) {
                                    if (enable_stream_restore && max_mock_len > 0) {
                                        // 提取 arguments 并追加到对应 index 的缓冲区
                                        std::string delta_args = func["arguments"].get<std::string>();
                                        auto &buffer_pair = restore_state->args_buffers[index];

                                        buffer_pair.first += delta_args;

                                        // 记录此 tool_call 所属的 choice index（首次出现时记录）
                                        if (buffer_pair.second == 0 && choice.contains("index") && choice["index"].is_number_integer()) {
                                            buffer_pair.second = choice["index"].get<int>();
                                        }

                                        size_t args_buffer_size = buffer_pair.first.size();

                                        // 计算安全发送长度
                                        // 将 `>=` 改为 `>`，与 content 逻辑保持一致：
                                        // 当 args_buffer_size == max_mock_len-1 时，safe_len 会为 0，
                                        // 进入此分支后 UTF-8 检查也会跳过，造成无意义的处理。
                                        // 使用严格大于 `>` 确保只有 safe_len > 0 时才进入此分支。
                                        if (args_buffer_size > max_mock_len - 1) {
                                            size_t safe_len = args_buffer_size - (max_mock_len - 1);

                                            // safe_len 最小值保护（与 content 逻辑对称）
                                            const size_t MIN_SAFE_LEN = max_mock_len / 2;

                                            // 与 content 相同，避免把 arguments 里的 mock key 前缀提前发送出去。
                                            while (safe_len > 0) {
                                                std::string candidate = buffer_pair.first.substr(0, safe_len);
                                                bool ends_with_mock_prefix = false;
                                                for (const auto &[mock, real] : mapping) {
                                                    size_t max_prefix = std::min(mock.size() - 1, candidate.size());
                                                    for (size_t prefix_len = max_prefix; prefix_len > 0; --prefix_len) {
                                                        if (candidate.size() >= prefix_len &&
                                                            candidate.compare(candidate.size() - prefix_len, prefix_len,
                                                                              mock, 0, prefix_len) == 0) {
                                                            ends_with_mock_prefix = true;
                                                            --safe_len;
                                                            break;
                                                        }
                                                    }
                                                    if (ends_with_mock_prefix) {
                                                        break;
                                                    }
                                                }
                                                if (!ends_with_mock_prefix) {
                                                    break;
                                                }
                                            }

                                            // 应用 MIN_SAFE_LEN 下限：prefix 回退后 safe_len 可能极小，
                                            // 若小于 MIN_SAFE_LEN 则不发送（等待更多内容累积），避免窗口余量不足。
                                            if (safe_len < MIN_SAFE_LEN) {
                                                func.erase("arguments");
                                                continue;  // 跳到 for (auto &tool_call : ...) 的下一次迭代
                                            }

                                            // 将截断点调整到 UTF-8 字符边界
                                            while (safe_len > 0 &&
                                                   (static_cast<unsigned char>(buffer_pair.first[safe_len]) & 0xC0) == 0x80) {
                                                --safe_len;
                                            }
                                            if (safe_len == 0) {
                                                // 整个安全区域都是续字节（极端情况），跳过发送此 tool_call 的 arguments
                                                func.erase("arguments");
                                                continue;  // 跳到 for (auto &tool_call : ...) 的下一次迭代
                                            }
                                            std::string safe_args = buffer_pair.first.substr(0, safe_len);
                                            safe_args = RestoreStringFully(safe_args, mapping);

                                            // 更新 arguments 为还原后的安全部分
                                            func["arguments"] = safe_args;
                                            has_content_to_send = true;

                                            // 保留窗口尾部
                                            buffer_pair.first = buffer_pair.first.substr(safe_len);
                                        } else {
                                            // 缓冲区不足：跳过发送此 chunk 的 arguments
                                            // 仅当 log_desensitization_details=true 时打印缓冲区原文（含原始敏感信息，生产环境默认关闭）
                                            if (routing_config_.desensitization.log_desensitization_details) {
                                                My_Log{} << "[GenieRoutingGateway] [ArgsWindowSkip] tool_call[" << index
                                                         << "] buffer too small (" << args_buffer_size << " < " << (max_mock_len - 1)
                                                         << "), ERASING arguments from chunk (will wait for Flush)"
                                                         << ", buffered_content=\"" << buffer_pair.first << "\""
                                                         << std::endl;
                                            }
                                            func.erase("arguments");
                                        }
                                    } else {
                                        has_content_to_send = true;

                                        std::string args_fragment = func["arguments"].get<std::string>();
                                        // 仅当 log_desensitization_details=true 时打印参数原文（含原始敏感信息，生产环境默认关闭）
                                        if (routing_config_.desensitization.log_desensitization_details) {
                                            My_Log{} << "[GenieRoutingGateway] [ArgsDirectSend] tool_call[" << index
                                                     << "] sending arguments directly (no buffering), length=" << args_fragment.size()
                                                     << ", content=\"" << args_fragment << "\"" << std::endl;
                                        }
                                    }
                                }
                            }
                        }
                    }

                    // 检查是否有任何值得发送的内容（包括 finish_reason）
                    bool should_send = has_content_to_send;
                    if (!should_send) {
                        // 检查是否有 finish_reason 或其他非空 delta 字段
                        for (const auto &choice : chunk_json["choices"]) {
                            if (choice.contains("finish_reason") && !choice["finish_reason"].is_null()) {
                                should_send = true;
                                break;
                            }
                            if (choice.contains("delta") && choice["delta"].is_object() && !choice["delta"].empty()) {
                                should_send = true;
                                break;
                            }
                        }
                    }

                    // 如果没有任何值得发送的内容，跳过此 chunk（返回空字符串，外层不会发送）
                    // 注意：此处不调用 lock.lock()，由外层循环末尾统一重新加锁，
                    // 与原 goto 版本中 continue 跳过循环末尾 lock.lock() 的语义等价：
                    //   原代码：lock.lock(); continue; → 重新加锁后跳回循环头（循环末尾的 lock.lock() 被跳过）
                    //   重构后：return ""; → lambda 返回空字符串，外层循环末尾执行 lock.lock()
                    // 两者都只加锁一次，行为完全等价。
                    if (!should_send) {
                        return std::string{};
                    }

                    // 序列化还原后的 chunk
                    return chunk_json.dump();
                } else {
                    // 配置禁用或无映射表：仍然需要解析chunk并直接发送,不做缓冲
                    // 不能直接 return chunk,因为这样会绕过所有处理逻辑
                    json chunk_json;
                    try {
                        chunk_json = json::parse(chunk);
                    } catch (const json::exception &e) {
                        // 解析失败：直接发送原始 chunk
                        return chunk;
                    }

                    // 防御性异常捕获：保护后续所有字段访问逻辑，
                    // 防止未来出现类似 null 字段访问导致 type_error 未捕获而崩溃。
                    try {
                    // 流式打印模型输出文本（无映射表路径）
                    if (chunk_json.contains("choices") && chunk_json["choices"].is_array()) {
                        for (const auto &choice : chunk_json["choices"]) {
                            if (choice.contains("delta") && choice["delta"].is_object()) {
                                const auto &delta = choice["delta"];
                                if (delta.contains("tool_calls") && delta["tool_calls"].is_array()) {
                                    // 工具调用：流式打印函数名和参数文本
                                    for (const auto &tc : delta["tool_calls"]) {
                                        if (tc.contains("function") && tc["function"].is_object()) {
                                            const auto &func_obj = tc["function"];
                                            // value() 只在键不存在时返回默认值；
                                            // 键存在但值为 null 时，内部仍调用 get<string>() 并抛出 type_error。
                                            // MiniMax-M2.5 等模型在工具调用的第一个 chunk 中会将 arguments 设为 null，
                                            // 必须先检查 is_string() 再取值，否则会导致 terminate()/abort() 崩溃。
                                            const std::string func_name = (func_obj.contains("name") && func_obj["name"].is_string())
                                                                          ? func_obj["name"].get<std::string>() : "";
                                            const std::string func_args = (func_obj.contains("arguments") && func_obj["arguments"].is_string())
                                                                          ? func_obj["arguments"].get<std::string>() : "";
                                            if (!func_name.empty())
                                                My_Log{}.original(true) << func_name;
                                            if (!func_args.empty())
                                                My_Log{}.original(true) << func_args;
                                        }
                                    }
                                } else if (delta.contains("content") && delta["content"].is_string()) {
                                    // 普通调用：流式打印 delta.content 文本
                                    const std::string content = delta["content"].get<std::string>();
                                    if (!content.empty())
                                        My_Log{}.original(true) << content;
                                }
                            }
                        }
                    }

                    // 直接序列化(不做任何修改)
                    return chunk_json.dump();
                    } catch (const json::exception &e) {
                        // 防御性兜底：捕获字段访问时的 JSON 异常（如 null 类型转换等），
                        // 降级为直接发送原始 chunk，避免异常传播到 httplib 导致 terminate()/abort()。
                        My_Log{My_Log::Level::kWarning}
                            << "[GenieRoutingGateway] JSON exception in SSE chunk processing (no-mapping path): "
                            << e.what() << ", chunk=" << chunk << std::endl;
                        return chunk;
                    }
                }
                return std::string{};  // 不可达，但满足编译器要求
                }();  // 立即调用 lambda，得到 restored_chunk

                // 检查 flush 阶段是否发生写入失败（客户端断开）
                // flush_write_failed 由内层 lambda 设置，此处在外层 provider lambda（返回 bool）中处理
                if (flush_write_failed) {
                    return false;
                }

                // 发送还原后的 chunk（或原始 chunk）
                if (!restored_chunk.empty()) {
                    std::string sse_line = "data: " + restored_chunk + "\n\n";

                    if (!s.write(sse_line.c_str(), sse_line.size()))
                    {
                        // 客户端断开连接：通知生产者停止接收云端数据，避免云端连接被异常中断。
                        // 设置 consumer_stopped 后，生产者的 on_chunk 回调在下次被调用时会返回 false，
                        // 触发 DoStreamRequest 中的 aborted=true 路径（正常退出，不报错）。
                        //
                        // ⚠️ 竞态条件说明：
                        // 若生产者在 consumer_stopped 被设置后、on_chunk 被调用前，
                        // 云端连接已经关闭（例如云端恰好在此时发送了最后的数据并关闭连接），
                        // 则 aborted 标志可能不会被设置，DoStreamRequest 会走到
                        // "SSE stream ended without [DONE]" 路径。
                        // 这是一个已知的边缘情况，属于非功能性问题（数据已正常发送给客户端）。
                        My_Log{} << "[GenieRoutingGateway] Consumer stopped (client disconnected), "
                                  << "notifying producer to abort cloud SSE reception" << std::endl;
                        std::lock_guard<std::mutex> notify_lock(state->mtx);
                        state->consumer_stopped = true;
                        state->cv.notify_all();
                        return false;
                    }
                }

                lock.lock();
            }

            if (state->done)
            {
                if (state->success)
                {
                    // 打印响应摘要，标识纯工具调用场景
                    bool is_pure_tool_call = state->accumulated_response.empty();
                    if (is_pure_tool_call) {
                        My_Log{} << "[GenieRoutingGateway] [PureToolCall] Pure tool_calls response detected: "
                                 << "accumulated_response is empty (no text content), "
                                 << "chunks=" << state->chunks.size()  // 当前队列中剩余的 chunk 数
                                 << ". This is normal for tool_calls without content field." << std::endl;
                    }

                    My_Log{} << "[GenieRoutingGateway] [StreamFlush] state->done triggered, enable_stream_restore="
                             << enable_stream_restore << ", mapping.empty=" << mapping.empty()
                             << ", content_buffer.size=" << restore_state->content_buffer.size()
                             << ", args_buffers.size=" << restore_state->args_buffers.size()
                             << std::endl;

                    if (enable_stream_restore && !mapping.empty()) {
                        My_Log{} << "[GenieRoutingGateway] [StreamFlush] Entering flush logic..." << std::endl;

                        // 1. Flush content_buffer
                        if (!restore_state->content_buffer.empty()) {
                            My_Log{} << "[GenieRoutingGateway] [StreamFlush] Flushing content_buffer, size="
                                     << restore_state->content_buffer.size()
                                     << ", preview=\"" << (restore_state->content_buffer.size() > 100
                                                          ? restore_state->content_buffer.substr(0, 100) + "..."
                                                          : restore_state->content_buffer) << "\""
                                     << std::endl;
                            // 对剩余内容执行还原
                            std::string remaining = RestoreStringFully(restore_state->content_buffer, mapping);

                            // 构建 chunk 发送剩余 content（使用记录的 choice index）
                            json final_chunk = {
                                {"choices", json::array({
                                    {
                                        {"delta", {{"content", remaining}}},
                                        {"index", restore_state->content_choice_index},
                                        {"finish_reason", json(nullptr)}
                                    }
                                })}
                            };
                            std::string sse_line = "data: " + final_chunk.dump() + "\n\n";
                            s.write(sse_line.c_str(), sse_line.size());

                            restore_state->content_buffer.clear();
                        }

                        // 2. Flush args_buffers（按 index 分别 flush）
                        My_Log{} << "[GenieRoutingGateway] [StreamFlush] Flushing args_buffers, count="
                                 << restore_state->args_buffers.size() << std::endl;

                        for (auto &[index, buffer_pair] : restore_state->args_buffers) {
                            if (buffer_pair.first.empty()) {
                                My_Log{} << "[GenieRoutingGateway] [StreamFlush] args_buffer[" << index
                                         << "] is empty, skipping" << std::endl;
                                continue;
                            }

                            My_Log{} << "[GenieRoutingGateway] [StreamFlush] Flushing args_buffer[" << index
                                     << "], size=" << buffer_pair.first.size()
                                     << ", preview=\"" << (buffer_pair.first.size() > 100
                                                          ? buffer_pair.first.substr(0, 100) + "..."
                                                          : buffer_pair.first) << "\""
                                     << std::endl;

                            // 对剩余 arguments 执行还原
                            // 注意：此路径（state->done 触发）通常在 finish_reason=tool_calls flush 之后执行，
                            // 彼时 args_buffers 已被清空，此循环体实际上不会被执行到。
                            // 保留此路径作为兜底，以防 finish_reason=tool_calls 未触发的极端情况。
                            std::string remaining_args = RestoreStringFully(buffer_pair.first, mapping);

                            // 构建 chunk 发送剩余 arguments
                            // 注意：choice.index 与 tool_call.index 不同：
                            //   - choice.index：choices 数组中的 choice 索引（通常为 0，多 choice 时才 > 0）
                            //   - tool_call.index：tool_calls 数组中的工具调用索引（即此处的 index 变量）
                            // 此处 choice.index 使用记录的 buffer_pair.second（此 tool_call 所属的 choice index）
                            json final_args_chunk = {
                                {"choices", json::array({
                                    {
                                        {"delta", {
                                            {"tool_calls", json::array({
                                                {
                                                    {"index", index},
                                                    {"function", {{"arguments", remaining_args}}}
                                                }
                                            })}
                                        }},
                                        {"index", buffer_pair.second},
                                        {"finish_reason", json(nullptr)}
                                    }
                                })}
                            };
                            std::string sse_line = "data: " + final_args_chunk.dump() + "\n\n";
                            s.write(sse_line.c_str(), sse_line.size());

                            buffer_pair.first.clear();
                        }
                        restore_state->args_buffers.clear();
                    }

                    // 流式响应已结束（完整正文已在逐 chunk 过程中打印过，此处仅记录完成状态，避免重复输出全文）
                    My_Log{} << "[GenieRoutingGateway] Cloud SSE stream completed successfully, "
                             << "response_length=" << state->accumulated_response.size() << std::endl;

                    // ── Token 限流更新（流式响应完成后）──────────────────────────────
                    // 注意：UpdateAndCheckTokenCount 在此处仅做记录，不能中断已发送的响应。
                    // 若超限，下一次推理请求会被 CheckAndIncrementInferenceCount 拦截。
                    if (state->has_token_data)
                    {
                        // 打印详细的 Token 消耗日志（分项显示 prompt + completion）
                        My_Log{} << "[CloudTokenUsage] session=" << session_id
                                 << ", mode=stream"
                                 << ", prompt_tokens=" << state->prompt_tokens
                                 << ", completion_tokens=" << state->completion_tokens
                                 << ", total_tokens=" << state->total_tokens
                                 << std::endl;
                        httplib::Response dummy_res;
                        this->UpdateAndCheckTokenCount(session_id, state->total_tokens, dummy_res,
                                                       state->prompt_tokens, state->completion_tokens, cloud_tier);
                    }
                    else
                    {
                        My_Log{My_Log::Level::kWarning}
                            << "[CloudTokenUsage] session=" << session_id
                            << ", mode=stream: no usage data in stream chunks (cannot track token consumption)"
                            << std::endl;
                    }

                    // 生产者成功完成：发送 [DONE] 标记
                    static const std::string done_line = "data: [DONE]\n\n";
                    s.write(done_line.c_str(), done_line.size());
                    s.done();
                }
                else
                {
                    // 流式响应失败但已累积部分内容（完整正文已在逐 chunk 过程中打印过，此处仅记录长度，避免重复输出全文）
                    if (!state->accumulated_response.empty())
                    {
                        My_Log{} << "[GenieRoutingGateway] Cloud SSE stream failed with partial response, "
                                 << "accumulated_length=" << state->accumulated_response.size() << std::endl;
                    }
                    // 生产者失败：不发送 [DONE]，直接关闭连接
                    // 区分非致命失败（消费者断开/代理截断）和真正的端点故障
                    static const std::string nonfatal_err = "all endpoints failed for streaming";
                    bool is_nonfatal = (state->error_msg == nonfatal_err ||
                                        state->error_msg.find("non-fatal") != std::string::npos);
                    if (is_nonfatal)
                    {
                        // 非致命：消费者断开或代理截断，数据已正常发送，降级为 WARNING
                        My_Log{My_Log::Level::kWarning}
                            << "[GenieRoutingGateway] Cloud SSE stream ended without [DONE] (non-fatal): "
                            << state->error_msg
                            << " -- data already delivered to consumer" << std::endl;
                    }
                    else
                    {
                        // 真正的端点故障
                        My_Log{My_Log::Level::kError}
                            << "[GenieRoutingGateway] Cloud SSE stream failed mid-stream: "
                            << state->error_msg << std::endl;
                    }
                    s.done();
                }
                return false;
            }

            return true;
          } catch (const std::exception &e) {
            // 捕获 provider lambda 内部的所有 std::exception（含 json::type_error 等），
            // 防止异常逃逸到 httplib 的 noexcept 边界导致 std::terminate()/abort()。
            // 典型场景：某些云端模型（如 MiniMax-M2.5）在工具调用 chunk 中将字段设为 null，
            // nlohmann::json::value() 在 key 存在但值为 null 时会抛出 json::type_error，
            // 若不捕获则会触发 terminate()。
            My_Log{My_Log::Level::kError}
                << "[GenieRoutingGateway] Exception in SSE provider lambda: "
                << e.what() << " -- closing stream gracefully" << std::endl;
            s.done();
            return false;
          } catch (...) {
            // 捕获所有其他未知异常（如 std::bad_alloc 等），确保不会逃逸到 noexcept 边界
            My_Log{My_Log::Level::kError}
                << "[GenieRoutingGateway] Unknown exception in SSE provider lambda"
                << " -- closing stream gracefully" << std::endl;
            s.done();
            return false;
          }
        };

        if (sink)
        {
            // 同步执行：调用方已在 set_chunked_content_provider 回调中，
            // 直接向现有 sink 写入云端 SSE 数据，无需重新设置 chunked provider。
            // 生产者线程已在上方 detach，此处循环驱动 provider 直到完成。
            My_Log{} << "[GenieRoutingGateway] Cloud SSE stream: synchronous execution into existing sink" << std::endl;
            // sink 路径：provider 内部会在第一次调用时发送初始状态（由调用方通过 initial_status 参数指定）
            bool cont = true;
            while (cont)
            {
                cont = provider(0, *sink);
            }
        }
        else
        {
            http_res.set_chunked_content_provider("text/event-stream", provider);
        }

        handled_by_cloud = true;
        return true;
    }
    else
    {
        json cloud_response;

        // 使用 active_client（由 cloud_tier 决定）
        if (!active_client.ChatCompletion(mutable_request, cloud_response, error_msg))
        {
            My_Log{My_Log::Level::kError} << "[GenieRoutingGateway] Cloud request failed: "
                                          << error_msg << std::endl;

            // 云端失败，根据 fallback 策略处理
            if (routing_config_.fallback.cloud_unavailable_to_local)
            {
                My_Log{} << "[GenieRoutingGateway] Falling back to local processing" << std::endl;
                handled_by_cloud = false;
                return true;
            }
            else
            {
                json err_resp = {
                    {"error", {
                        {"message", "Cloud model request failed: " + error_msg},
                        {"type", "cloud_error"},
                        {"code", "cloud_request_failed"}
                    }}
                };
                http_res.status = 502;
                http_res.set_content(err_resp.dump(), ResponseDispatcher::MIMETYPE_JSON);
                // 502 错误：云端连接失败，建议客户端稍后重试
                http_res.set_header("Retry-After", "30");
                return false;
            }
        }

        // 获取映射表并还原响应（仅当配置启用时）
        if (routing_config_.desensitization.restore_response_enabled) {
            auto mapping = GetDesensitizationMapping(user_session_id);
            if (!mapping.empty()) {
                RestoreCloudResponse(cloud_response, mapping);
            }
        }

        // 打印云端非 streaming 响应日志（与本地推理保持一致，使用 original 模式不带前缀）
        try
        {
            std::string response_text;
            if (cloud_response.contains("choices") && cloud_response["choices"].is_array() &&
                !cloud_response["choices"].empty())
            {
                const auto &choice = cloud_response["choices"][0];
                if (choice.contains("message") && choice["message"].contains("content") &&
                    choice["message"]["content"].is_string())
                {
                    response_text = choice["message"]["content"].get<std::string>();
                }
                else
                {
                    response_text = cloud_response.dump(2);
                }
            }
            else
            {
                response_text = cloud_response.dump(2);
            }
            My_Log{}.original(true) << response_text;
            My_Log{} << "\n------------\n" << std::endl;
        }
        catch (const std::exception &e)
        {
            My_Log{} << "(failed to format cloud response: " << e.what() << ")\n------------\n" << std::endl;
        }

        // ── Token 限流更新（非流式响应完成后）──────────────────────────────
        // 更新 Token 计数，供下一次推理请求的限流检查使用
        {
            auto usage = ExtractResponseTokenUsage(cloud_response);
            if (usage.has_data)
            {
                // 打印详细的 Token 消耗日志（分项显示 prompt + completion）
                My_Log{} << "[CloudTokenUsage] session=" << session_id
                         << ", mode=non-stream"
                         << ", prompt_tokens=" << usage.prompt_tokens
                         << ", completion_tokens=" << usage.completion_tokens
                         << ", total_tokens=" << usage.total_tokens
                         << std::endl;
                httplib::Response dummy_res;
                UpdateAndCheckTokenCount(session_id, usage.total_tokens, dummy_res,
                                         usage.prompt_tokens, usage.completion_tokens, cloud_tier);
            }
            else
            {
                My_Log{My_Log::Level::kWarning}
                    << "[CloudTokenUsage] session=" << session_id
                    << ", mode=non-stream: no usage data in response (cannot track token consumption)"
                    << std::endl;
            }
        }

        HandleCloudResponse(cloud_response, http_res);
        handled_by_cloud = true;
        return true;
    }
}
