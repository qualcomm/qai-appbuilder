//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "gateway.h"
#include "log.h"
#include "../../response/response_dispatcher.h"
#include "../../response/response_tools.h"
#include "../security/security_utils.h"
#include <chrono>
#include <sstream>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <algorithm>
#include <map>
#include <limits>

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

                    if (!has_non_null_content)
                    {

                        oss << "<|start|>" << role << "<|message|>" << msg["tool_calls"].dump() << "\n";
                    }
                    else
                    {

                        oss << "<|tool_calls|>" << msg["tool_calls"].dump() << "\n";
                    }
                }
                else if (!has_non_null_content && !msg.contains("tool_calls"))
                {

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

                if (delta.contains("content") && delta["content"].is_string())
                {
                    return delta["content"].get<std::string>();
                }

                if (delta.contains("tool_calls") && delta["tool_calls"].is_array())
                {
                    std::string tool_text;
                    for (const auto &tc : delta["tool_calls"])
                    {
                        if (tc.contains("function"))
                        {
                            const auto &func = tc["function"];

                            if (func.contains("name") && func["name"].is_string())
                            {
                                std::string name = func["name"].get<std::string>();
                                if (!name.empty())
                                    tool_text += "[tool_call:" + name + "]";
                            }

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

    }
    return "";
}

struct TokenUsageDetail {
    int64_t prompt_tokens = 0;
    int64_t completion_tokens = 0;
    int64_t total_tokens = 0;
    bool has_data = false;
};

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

    }
    return detail;
}

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

    }
    return detail;
}

std::string GenieRoutingGateway::RestoreStringFully(
        const std::string &text,
        const std::unordered_map<std::string, std::string> &mapping) const
{
    if (text.empty() || mapping.empty()) {
        return text;
    }

    std::string current = text;

    std::vector<std::pair<std::string, std::string>> ordered_mapping(
        mapping.begin(), mapping.end());
    std::sort(ordered_mapping.begin(), ordered_mapping.end(),
              [](const auto &a, const auto &b) {
                  if (a.first.size() != b.first.size()) {
                      return a.first.size() > b.first.size();
                  }
                  return a.first < b.first;
              });

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

void GenieRoutingGateway::SendPolicyViolationError(httplib::Response &res,
                                           const std::string &code,
                                           const std::string &message,
                                           int http_status,
                                           bool is_stream)
{

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

        std::string sse_body = "data: " + error_response.dump() + "\n\ndata: [DONE]\n\n";
        res.status = 200;
        res.set_content(sse_body, "text/event-stream");
        My_Log{} << "[GenieRoutingGateway] Sent SSE error response (code=" << code
                 << ", http_status=" << http_status << ")" << std::endl;
    }
    else
    {
        res.status = http_status;
        res.set_content(error_response.dump(), ResponseDispatcher::MIMETYPE_JSON);

        if (http_status == 503)
        {
            res.set_header("Retry-After", "60");
            My_Log{} << "[GenieRoutingGateway] Added Retry-After: 60 to 503 response (code=" << code << ")" << std::endl;
        }
    }
}

bool GenieRoutingGateway::HandleCloudResponse(const json &cloud_response,
                                      httplib::Response &http_res)
{

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

    auto restore_string = [this, &mapping](std::string &text) {
        text = RestoreStringFully(text, mapping);
    };

    constexpr int MAX_RECURSION_DEPTH = 50;

    std::function<void(json&, int)> restore_json_recursive = [&](json &node, int depth) {

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

    for (auto &choice : response["choices"]) {
        if (!choice.contains("message")) {
            continue;
        }

        auto &message = choice["message"];

        if (message.contains("content") && message["content"].is_string()) {
            std::string content = message["content"].get<std::string>();
            restore_string(content);
            message["content"] = content;
        }

        if (message.contains("tool_calls") && message["tool_calls"].is_array()) {
            for (auto &tool_call : message["tool_calls"]) {
                if (!tool_call.contains("function")) {
                    continue;
                }

                auto &func = tool_call["function"];
                if (func.contains("arguments")) {

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

    CloudModelClient &active_client = (cloud_tier == CloudTier::ENTERPRISE)
        ? enterprise_cloud_client_
        : cloud_client_;
    const char *tier_label = (cloud_tier == CloudTier::ENTERPRISE) ? "Enterprise" : "Public";

    const std::string &session_id = user_session_id;
    if (!CheckAndIncrementInferenceCount(session_id, http_res, cloud_tier))
    {

        handled_by_cloud = true;
        return true;
    }

    {
        std::string cloud_system_prompt;
        if (cloud_request.contains("messages") && cloud_request["messages"].is_array()) {
            for (const auto& msg : cloud_request["messages"]) {
                if (msg.is_object() && msg.value("role", "") == "system") {
                    cloud_system_prompt = SecurityUtils::ExtractMessageContentText(msg);
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

        bool cloud_log_debug = (cloud_tier == CloudTier::ENTERPRISE)
            ? enterprise_cloud_config_.log_debug
            : cloud_config_.log_debug;
        if (cloud_log_debug) {
            My_Log{} << "[CloudRequest][JSON] " << cloud_request.dump() << std::endl;
        }
    }

    json mutable_request = cloud_request;
    {

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
            bool done = false;
            bool success = false;
            bool consumer_stopped = false;
            std::string error_msg;
            std::string accumulated_response;

            int64_t total_tokens = -1;
            int64_t prompt_tokens = 0;
            int64_t completion_tokens = 0;
            bool has_token_data = false;
        };
        auto state = std::make_shared<StreamState>();

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
            }
            else
            {
                std::string err;
                bool ok = cloud_client_ptr->ChatCompletionStream(
                    mutable_request,
                    [&state](const std::string &chunk) -> bool {
                        {
                            std::lock_guard<std::mutex> lock(state->mtx);

                            if (state->consumer_stopped)
                            {
                                return false;
                            }
                            state->chunks.push_back(chunk);

                            std::string delta_text = ExtractDeltaContent(chunk);
                            if (!delta_text.empty()) {
                                state->accumulated_response += delta_text;
                            }

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
            }
        });
        producer.detach();

        {
            std::unique_lock<std::mutex> lock(state->mtx);
            state->cv.wait_for(lock,
                std::chrono::seconds(30),
                [&state]{ return !state->chunks.empty() || state->done; });

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

                    http_res.set_header("Retry-After", "30");
                    return false;
                }
            }
        }

        auto status_sent = std::make_shared<bool>(false);

        bool enable_stream_restore = routing_config_.desensitization.restore_response_enabled &&
                                     routing_config_.desensitization.restore_stream_enabled;
        auto mapping = enable_stream_restore
                        ? GetDesensitizationMapping(session_id)
                        : std::unordered_map<std::string, std::string>{};

        My_Log{} << "[GenieRoutingGateway] [RESTORE-CONFIG] session_id=" << session_id
                 << ", enable_stream_restore=" << enable_stream_restore
                 << ", mapping.size=" << mapping.size()
                 << std::endl;
        if (!mapping.empty() && mapping.size() <= 5) {

            for (const auto &[mock, real] : mapping) {
                My_Log{} << "[GenieRoutingGateway] [RESTORE-CONFIG] mapping: \"" << mock
                         << "\" -> \"" << real << "\"" << std::endl;
            }
        }

        size_t max_mock_len = 0;
        if (enable_stream_restore && !mapping.empty()) {
            for (const auto &[mock, real] : mapping) {
                max_mock_len = std::max(max_mock_len, mock.size());
            }
        }

        struct StreamRestoreState {
            std::string content_buffer;
            std::map<int, std::pair<std::string, int>> args_buffers;
            int content_choice_index = 0;
        };
        auto restore_state = std::make_shared<StreamRestoreState>();

        auto provider = [this, state, session_id, status_sent, initial_status, initial_message, mapping, enable_stream_restore, max_mock_len, restore_state, cloud_tier](size_t /*offset*/, httplib::DataSink &s) -> bool {
          try {

            if (!*status_sent) {
                *status_sent = true;
                ResponseTools::post_stream_data(s, "data",
                    ResponseTools::statusDataJson(initial_status, initial_message));
            }

            std::unique_lock<std::mutex> lock(state->mtx);

            state->cv.wait_for(lock,
                std::chrono::seconds(60),
                [&state]{ return !state->chunks.empty() || state->done; });

            while (!state->chunks.empty())
            {
                std::string chunk = std::move(state->chunks.front());
                state->chunks.erase(state->chunks.begin());
                lock.unlock();

                bool flush_write_failed = false;
                std::string restored_chunk = [&]() -> std::string {
                if (enable_stream_restore && !mapping.empty() && max_mock_len > 0) {

                    json chunk_json;
                    try {
                        chunk_json = json::parse(chunk);
                    } catch (const json::exception &e) {

                        return chunk;
                    }

                    if (!chunk_json.contains("choices") || !chunk_json["choices"].is_array()) {

                        return chunk;
                    }

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
                                        func_pre["arguments"] = "";
                                    }
                                }
                            }
                        }

                        My_Log{} << "[GenieRoutingGateway] [ToolCallFlush] Detected finish_reason=tool_calls, "
                                 << "args_buffers.size=" << restore_state->args_buffers.size()
                                 << ", flushing all buffers before sending finish chunk" << std::endl;

                        for (auto &[index, buffer_pair] : restore_state->args_buffers) {
                            if (buffer_pair.first.empty()) {
                                continue;
                            }

                            std::string remaining_args = buffer_pair.first;
                            std::string before_restore = remaining_args;
                            remaining_args = RestoreStringFully(remaining_args, mapping);

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

                    bool has_content_to_send = false;
                    for (auto &choice : chunk_json["choices"]) {
                        if (!choice.contains("delta") || !choice["delta"].is_object()) {
                            continue;
                        }

                        auto &delta = choice["delta"];

                        if (delta.contains("content") && delta["content"].is_string()) {
                            if (enable_stream_restore && max_mock_len > 0) {

                                std::string delta_content = delta["content"].get<std::string>();
                                restore_state->content_buffer += delta_content;

                                if (choice.contains("index") && choice["index"].is_number_integer()) {
                                    restore_state->content_choice_index = choice["index"].get<int>();
                                }

                                size_t buffer_size = restore_state->content_buffer.size();

                                if (buffer_size > max_mock_len - 1) {
                                    size_t safe_len = buffer_size - (max_mock_len - 1);

                                    const size_t MIN_SAFE_LEN = max_mock_len / 2;

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

                                    if (safe_len < MIN_SAFE_LEN) {
                                        delta.erase("content");
                                        continue;
                                    }

                                    while (safe_len > 0 &&
                                           (static_cast<unsigned char>(restore_state->content_buffer[safe_len]) & 0xC0) == 0x80) {
                                        --safe_len;
                                    }
                                    if (safe_len == 0) {

                                        delta.erase("content");
                                        continue;
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

                                    delta["content"] = safe_content;
                                    has_content_to_send = true;

                                    restore_state->content_buffer = restore_state->content_buffer.substr(safe_len);
                                } else {

                                    delta.erase("content");
                                }
                            } else {
                                has_content_to_send = true;
                            }
                        }

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

                                        std::string delta_args = func["arguments"].get<std::string>();
                                        auto &buffer_pair = restore_state->args_buffers[index];

                                        buffer_pair.first += delta_args;

                                        if (buffer_pair.second == 0 && choice.contains("index") && choice["index"].is_number_integer()) {
                                            buffer_pair.second = choice["index"].get<int>();
                                        }

                                        size_t args_buffer_size = buffer_pair.first.size();

                                        if (args_buffer_size > max_mock_len - 1) {
                                            size_t safe_len = args_buffer_size - (max_mock_len - 1);

                                            const size_t MIN_SAFE_LEN = max_mock_len / 2;

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

                                            if (safe_len < MIN_SAFE_LEN) {
                                                func.erase("arguments");
                                                continue;
                                            }

                                            while (safe_len > 0 &&
                                                   (static_cast<unsigned char>(buffer_pair.first[safe_len]) & 0xC0) == 0x80) {
                                                --safe_len;
                                            }
                                            if (safe_len == 0) {

                                                func.erase("arguments");
                                                continue;
                                            }
                                            std::string safe_args = buffer_pair.first.substr(0, safe_len);
                                            safe_args = RestoreStringFully(safe_args, mapping);

                                            func["arguments"] = safe_args;
                                            has_content_to_send = true;

                                            buffer_pair.first = buffer_pair.first.substr(safe_len);
                                        } else {

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

                    bool should_send = has_content_to_send;
                    if (!should_send) {

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

                    if (!should_send) {
                        return std::string{};
                    }

                    return chunk_json.dump();
                } else {

                    json chunk_json;
                    try {
                        chunk_json = json::parse(chunk);
                    } catch (const json::exception &e) {

                        return chunk;
                    }

                    try {

                    if (chunk_json.contains("choices") && chunk_json["choices"].is_array()) {
                        for (const auto &choice : chunk_json["choices"]) {
                            if (choice.contains("delta") && choice["delta"].is_object()) {
                                const auto &delta = choice["delta"];
                                if (delta.contains("tool_calls") && delta["tool_calls"].is_array()) {

                                    for (const auto &tc : delta["tool_calls"]) {
                                        if (tc.contains("function") && tc["function"].is_object()) {
                                            const auto &func_obj = tc["function"];

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

                                    const std::string content = delta["content"].get<std::string>();
                                    if (!content.empty())
                                        My_Log{}.original(true) << content;
                                }
                            }
                        }
                    }

                    return chunk_json.dump();
                    } catch (const json::exception &e) {

                        My_Log{My_Log::Level::kWarning}
                            << "[GenieRoutingGateway] JSON exception in SSE chunk processing (no-mapping path): "
                            << e.what() << ", chunk=" << chunk << std::endl;
                        return chunk;
                    }
                }
                return std::string{};
                }();

                if (flush_write_failed) {
                    return false;
                }

                if (!restored_chunk.empty()) {
                    std::string sse_line = "data: " + restored_chunk + "\n\n";

                    if (!s.write(sse_line.c_str(), sse_line.size()))
                    {

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

                    bool is_pure_tool_call = state->accumulated_response.empty();
                    if (is_pure_tool_call) {
                        My_Log{} << "[GenieRoutingGateway] [PureToolCall] Pure tool_calls response detected: "
                                 << "accumulated_response is empty (no text content), "
                                 << "chunks=" << state->chunks.size()
                                 << ". This is normal for tool_calls without content field." << std::endl;
                    }

                    My_Log{} << "[GenieRoutingGateway] [StreamFlush] state->done triggered, enable_stream_restore="
                             << enable_stream_restore << ", mapping.empty=" << mapping.empty()
                             << ", content_buffer.size=" << restore_state->content_buffer.size()
                             << ", args_buffers.size=" << restore_state->args_buffers.size()
                             << std::endl;

                    if (enable_stream_restore && !mapping.empty()) {
                        My_Log{} << "[GenieRoutingGateway] [StreamFlush] Entering flush logic..." << std::endl;

                        if (!restore_state->content_buffer.empty()) {
                            My_Log{} << "[GenieRoutingGateway] [StreamFlush] Flushing content_buffer, size="
                                     << restore_state->content_buffer.size()
                                     << ", preview=\"" << (restore_state->content_buffer.size() > 100
                                                          ? restore_state->content_buffer.substr(0, 100) + "..."
                                                          : restore_state->content_buffer) << "\""
                                     << std::endl;

                            std::string remaining = RestoreStringFully(restore_state->content_buffer, mapping);

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

                            std::string remaining_args = RestoreStringFully(buffer_pair.first, mapping);

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

                    My_Log{} << "[GenieRoutingGateway] Cloud SSE stream completed successfully, "
                             << "response_length=" << state->accumulated_response.size() << std::endl;

                    if (state->has_token_data)
                    {

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

                    static const std::string done_line = "data: [DONE]\n\n";
                    s.write(done_line.c_str(), done_line.size());
                    s.done();
                }
                else
                {

                    if (!state->accumulated_response.empty())
                    {
                        My_Log{} << "[GenieRoutingGateway] Cloud SSE stream failed with partial response, "
                                 << "accumulated_length=" << state->accumulated_response.size() << std::endl;
                    }

                    static const std::string nonfatal_err = "all endpoints failed for streaming";
                    bool is_nonfatal = (state->error_msg == nonfatal_err ||
                                        state->error_msg.find("non-fatal") != std::string::npos);
                    if (is_nonfatal)
                    {

                        My_Log{My_Log::Level::kWarning}
                            << "[GenieRoutingGateway] Cloud SSE stream ended without [DONE] (non-fatal): "
                            << state->error_msg
                            << " -- data already delivered to consumer" << std::endl;
                    }
                    else
                    {

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

            My_Log{My_Log::Level::kError}
                << "[GenieRoutingGateway] Exception in SSE provider lambda: "
                << e.what() << " -- closing stream gracefully" << std::endl;
            s.done();
            return false;
          } catch (...) {

            My_Log{My_Log::Level::kError}
                << "[GenieRoutingGateway] Unknown exception in SSE provider lambda"
                << " -- closing stream gracefully" << std::endl;
            s.done();
            return false;
          }
        };

        if (sink)
        {

            My_Log{} << "[GenieRoutingGateway] Cloud SSE stream: synchronous execution into existing sink" << std::endl;

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

        if (!active_client.ChatCompletion(mutable_request, cloud_response, error_msg))
        {
            My_Log{My_Log::Level::kError} << "[GenieRoutingGateway] Cloud request failed: "
                                          << error_msg << std::endl;

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

                http_res.set_header("Retry-After", "30");
                return false;
            }
        }

        if (routing_config_.desensitization.restore_response_enabled) {
            auto mapping = GetDesensitizationMapping(user_session_id);
            if (!mapping.empty()) {
                RestoreCloudResponse(cloud_response, mapping);
            }
        }

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

        {
            auto usage = ExtractResponseTokenUsage(cloud_response);
            if (usage.has_data)
            {

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

bool GenieRoutingGateway::ExecuteCloudTierCase(CloudTier tier,
                                                bool need_desensitize,
                                                const json &original_request,
                                                const DesensitizationResult &desensitized,
                                                httplib::Response &http_res,
                                                bool &handled_by_cloud,
                                                const std::string &user_session_id)
{
    const bool is_enterprise = (tier == CloudTier::ENTERPRISE);
    const RouteTarget route_target = is_enterprise ? RouteTarget::ENTERPRISE_CLOUD : RouteTarget::PUBLIC_CLOUD;
    const char *route_name = is_enterprise ? "ENTERPRISE_CLOUD" : "PUBLIC_CLOUD";
    const char *cloud_desc = is_enterprise ? "enterprise cloud model" : "public cloud model";
    const char *cloud_label = is_enterprise ? "enterprise cloud" : "public cloud";
    const char *policy_log_prefix = is_enterprise ? "Enterprise cloud prompt policy: " : "Public cloud prompt policy: ";
    const char *prepared_log_prefix = is_enterprise ? "Enterprise cloud prompt prepared: " : "Public cloud prompt prepared: ";
    const char *failed_log_prefix = is_enterprise ? "Enterprise cloud prompt preparation failed: " : "Public cloud prompt preparation failed: ";
    const char *initial_message = is_enterprise ? "正在企业云推理..." : "正在公有云推理...";

    My_Log{} << "[GenieRoutingGateway] Route=" << route_name << ", sending to " << cloud_desc << std::endl;

    if (need_desensitize && !desensitized.success)
    {
        My_Log{My_Log::Level::kError}
            << "[GenieRoutingGateway] SECURITY GUARD: need_desensitize=true but desensitization failed. "
            << "Refusing to send original request to " << cloud_label << ". Falling back to local." << std::endl;
        handled_by_cloud = false;
        return true;
    }

    const json &base_request = (need_desensitize && desensitized.success)
                                 ? desensitized.desensitized_request
                                 : original_request;

    bool is_stream = false;
    if (original_request.contains("stream") && original_request["stream"].is_boolean())
    {
        is_stream = original_request["stream"].get<bool>();
    }

    PromptProcessingPolicy policy = ResolvePromptPolicy(client_source_, route_target);
    const char* policy_str = (policy == PromptProcessingPolicy::OPTIMIZED)
                               ? "OPTIMIZED" : "PASS_THROUGH";
    My_Log{} << "[GenieRoutingGateway] " << policy_log_prefix << policy_str << std::endl;

    json final_request = base_request;
    if (policy == PromptProcessingPolicy::OPTIMIZED)
    {

        int ctx_size = is_enterprise
            ? ((enterprise_cloud_config_.context_size > 0)
                   ? enterprise_cloud_config_.context_size
                   : EnterpriseCloudModelConfig::DEFAULT_ENTERPRISE_CLOUD_CONTEXT_SIZE)
            : ((cloud_config_.context_size > 0)
                   ? cloud_config_.context_size
                   : CloudModelConfig::DEFAULT_CLOUD_CONTEXT_SIZE);

        auto prep_result = prompt_prep_service_.PrepareForCloud(base_request, ctx_size);
        if (prep_result.success)
        {
            final_request = prep_result.prepared_request;
            My_Log{} << "[GenieRoutingGateway] " << prepared_log_prefix
                     << "system_optimized=" << prep_result.prompt_optimized
                     << ", messages_trimmed=" << prep_result.messages_trimmed
                     << ", context_size=" << prep_result.used_context_size << std::endl;
        }
        else
        {
            My_Log{My_Log::Level::kWarning}
                << "[GenieRoutingGateway] " << failed_log_prefix
                << prep_result.failure_reason
                << ". Falling back to original request." << std::endl;
        }
    }

    return ExecuteCloudRequest(final_request, is_stream, http_res,
                               handled_by_cloud, user_session_id,
                               nullptr, "inference", initial_message, tier);
}
