//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "gateway.h"
#include "log.h"
#include "../../response/response_dispatcher.h"
#include "../../chat_request_handler/task_memo_builder.h"
#include <chrono>
#include <sstream>
#include <iomanip>
#include <random>
#include <mutex>

std::string GenieRoutingGateway::GenerateRequestId()
{

    auto now = std::chrono::system_clock::now();
    auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        now.time_since_epoch()).count();

    static thread_local std::mt19937 rng(std::random_device{}());
    std::uniform_int_distribution<int> dist(1000, 9999);

    std::ostringstream oss;
    oss << "req_" << ms << "_" << dist(rng);
    return oss.str();
}

std::string GenieRoutingGateway::GenerateSessionId()
{

    auto now = std::chrono::system_clock::now();
    auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        now.time_since_epoch()).count();

    static thread_local std::mt19937 rng(std::random_device{}());
    std::uniform_int_distribution<uint32_t> dist(10000, 99999);

    std::ostringstream oss;
    oss << "sess_" << ms << "_" << dist(rng);
    return oss.str();
}

std::string GenieRoutingGateway::ComputeHistoryFingerprint(const json &messages)
{

    if (!messages.is_array() || messages.empty())
        return "";

    std::vector<const json*> non_system;
    for (const auto &msg : messages)
    {
        const std::string role = msg.value("role", "");
        if (role != "system" && role != "developer")
            non_system.push_back(&msg);
    }

    if (non_system.size() <= 1)
        return "";

    uint64_t hash = 14695981039346656037ULL;
    const uint64_t prime = 1099511628211ULL;

    auto hash_string = [&](const std::string &s) {
        for (unsigned char c : s) {
            hash ^= c;
            hash *= prime;
        }

        hash ^= 0xFF;
        hash *= prime;
    };

    for (size_t i = 0; i < non_system.size() - 1; ++i)
    {
        const auto &msg = *non_system[i];
        hash_string(msg.value("role", ""));

        if (msg.contains("content"))
        {
            if (msg["content"].is_string())
            {
                hash_string(msg["content"].get<std::string>());
            }
            else
            {

                hash_string(msg["content"].dump());
            }
        }
    }

    std::ostringstream oss;
    oss << std::hex << std::setfill('0') << std::setw(16) << hash;
    return oss.str();
}

static std::string ComputeFirstMsgKey(const std::vector<const json*> &non_system, uint64_t scope)
{
    if (non_system.empty() || non_system[0]->value("role", "") != "user")
        return "";

    uint64_t h = 14695981039346656037ULL;
    const uint64_t p = 1099511628211ULL;

    for (unsigned char c : std::string("user")) { h ^= c; h *= p; }
    h ^= 0xFF; h *= p;

    std::string content_str;
    if (non_system[0]->contains("content") && (*non_system[0])["content"].is_string())
        content_str = (*non_system[0])["content"].get<std::string>();
    else if (non_system[0]->contains("content"))
        content_str = (*non_system[0])["content"].dump();
    for (unsigned char c : content_str) { h ^= c; h *= p; }
    h ^= 0xFF; h *= p;

    if (scope != 0) { h ^= scope; h *= p; }

    std::ostringstream oss;
    oss << "fmk_" << std::hex << std::setfill('0') << std::setw(16) << h;
    return oss.str();
}

static void RegisterSessionEntry(std::unordered_map<std::string, ServerSessionEntry> &sessions,
                                  const ServerSessionEntry &entry,
                                  const std::vector<std::string> &keys)
{
    for (const auto &key : keys)
    {
        if (!key.empty())
            sessions[key] = entry;
    }
}

void GenieRoutingGateway::ResolveServerSessionIds(const json &request, InspectionContext &ctx)
{

    const json &messages = request.contains("messages") && request["messages"].is_array()
                           ? request["messages"]
                           : json::array();

    std::vector<const json*> non_system;
    for (const auto &msg : messages)
    {
        const std::string role = msg.value("role", "");
        if (role != "system" && role != "developer")
            non_system.push_back(&msg);
    }

    bool is_new_global_session = (non_system.size() == 1 &&
                                   non_system[0]->value("role", "") == "user");

    bool is_new_user_turn = (!non_system.empty() &&
                              non_system.back()->value("role", "") == "user");

    std::string fingerprint   = ComputeHistoryFingerprint(messages);
    std::string first_msg_key = ComputeFirstMsgKey(non_system, TaskMemoBuilder::ComputeScopeHash(messages, &request));

    std::lock_guard<std::mutex> lock(server_sessions_mutex_);

    const int ttl_seconds = (routing_config_.sticky_routing.ttl_seconds > 0)
                            ? routing_config_.sticky_routing.ttl_seconds
                            : 3600;
    auto now_tp = std::chrono::steady_clock::now();
    for (auto it = server_sessions_.begin(); it != server_sessions_.end(); )
    {
        auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(
            now_tp - it->second.last_updated).count();
        if (elapsed > ttl_seconds)
            it = server_sessions_.erase(it);
        else
            ++it;
    }

    if (is_new_global_session)
    {

        ServerSessionEntry entry;
        entry.global_session_id = GenerateSessionId();
        entry.user_session_id   = GenerateSessionId();
        entry.last_updated      = now_tp;

        RegisterSessionEntry(server_sessions_, entry, {first_msg_key, entry.user_session_id});

        ctx.session_id        = entry.user_session_id;
        ctx.global_session_id = entry.global_session_id;

        My_Log{} << "[GenieRoutingGateway] New global session detected (single user message): "
                 << "global_session_id=" << entry.global_session_id
                 << ", user_session_id=" << entry.user_session_id
                 << ", first_msg_key=" << (first_msg_key.empty() ? "(empty)" : first_msg_key)
                 << std::endl;
    }
    else if (fingerprint.empty())
    {

        ServerSessionEntry entry;
        entry.global_session_id = GenerateSessionId();
        entry.user_session_id   = GenerateSessionId();
        entry.last_updated      = now_tp;
        RegisterSessionEntry(server_sessions_, entry, {entry.user_session_id});

        ctx.session_id        = entry.user_session_id;
        ctx.global_session_id = entry.global_session_id;

        My_Log{} << "[GenieRoutingGateway] No history fingerprint (empty or single non-user message): "
                 << "using ephemeral session IDs, user_session_id=" << ctx.session_id
                 << std::endl;
    }
    else
    {

        auto it = server_sessions_.find(fingerprint);
        if (it == server_sessions_.end() && !first_msg_key.empty())
            it = server_sessions_.find(first_msg_key);

        if (it != server_sessions_.end())
        {

            ServerSessionEntry found_entry = it->second;
            found_entry.last_updated = now_tp;

            if (is_new_user_turn)
            {

                std::string old_uid = found_entry.user_session_id;
                found_entry.user_session_id = GenerateSessionId();

                RegisterSessionEntry(server_sessions_, found_entry, {fingerprint, first_msg_key, found_entry.user_session_id});

                ctx.session_id        = found_entry.user_session_id;
                ctx.global_session_id = found_entry.global_session_id;

                My_Log{} << "[GenieRoutingGateway] New user turn: "
                         << "global_session_id=" << ctx.global_session_id
                         << ", new user_session_id=" << ctx.session_id
                         << " (replaced " << old_uid << ")"
                         << std::endl;
            }
            else
            {

                RegisterSessionEntry(server_sessions_, found_entry, {fingerprint, first_msg_key, found_entry.user_session_id});

                ctx.session_id        = found_entry.user_session_id;
                ctx.global_session_id = found_entry.global_session_id;

                My_Log{} << "[GenieRoutingGateway] Tool call turn (continuing session): "
                         << "global_session_id=" << ctx.global_session_id
                         << ", user_session_id=" << ctx.session_id
                         << std::endl;
            }
        }
        else
        {

            ServerSessionEntry entry;
            entry.global_session_id = GenerateSessionId();
            entry.user_session_id   = GenerateSessionId();
            entry.last_updated      = now_tp;
            RegisterSessionEntry(server_sessions_, entry, {fingerprint, first_msg_key, entry.user_session_id});

            ctx.session_id        = entry.user_session_id;
            ctx.global_session_id = entry.global_session_id;

            My_Log{} << "[GenieRoutingGateway] New session entry created (no prior state): "
                     << "global_session_id=" << entry.global_session_id
                     << ", user_session_id=" << entry.user_session_id
                     << ", fingerprint=" << fingerprint
                     << std::endl;
        }
    }
}

bool GenieRoutingGateway::CheckAndIncrementInferenceCount(const std::string &session_id,
                                                           httplib::Response &http_res,
                                                           CloudTier cloud_tier)
{

    const int max_inferences = (cloud_tier == CloudTier::ENTERPRISE)
        ? enterprise_cloud_config_.rate_limit.max_inferences_per_task
        : cloud_config_.rate_limit.max_inferences_per_task;
    const int max_tokens = (cloud_tier == CloudTier::ENTERPRISE)
        ? enterprise_cloud_config_.rate_limit.max_tokens_per_task
        : cloud_config_.rate_limit.max_tokens_per_task;

    if (session_id.empty())
        return true;

    std::lock_guard<std::mutex> lock(rate_limit_mutex_);

    {
        const int ttl_seconds = (routing_config_.sticky_routing.ttl_seconds > 0)
                                ? routing_config_.sticky_routing.ttl_seconds
                                : 3600;
        auto now_tp = std::chrono::steady_clock::now();
        for (auto it = rate_limit_counters_.begin(); it != rate_limit_counters_.end(); )
        {
            auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(
                now_tp - it->second.last_updated).count();
            if (elapsed > ttl_seconds && it->first != session_id)
            {
                My_Log{} << "[GenieRoutingGateway] Rate limit counter expired and cleaned: session="
                         << it->first
                         << ", inference_count=" << it->second.inference_count
                         << ", total_tokens=" << it->second.total_tokens << std::endl;
                it = rate_limit_counters_.erase(it);
            }
            else
            {
                ++it;
            }
        }
    }

    auto &counter = rate_limit_counters_[session_id];
    counter.last_updated = std::chrono::steady_clock::now();

    if (max_inferences > 0 && counter.inference_count >= max_inferences)
    {
        My_Log{My_Log::Level::kError}
            << "[GenieRoutingGateway] Rate limit exceeded: session=" << session_id
            << ", inference_count=" << counter.inference_count
            << " >= max_inferences_per_task=" << max_inferences
            << ". Returning error to client." << std::endl;

        json error_resp = {
            {"error", {
                {"message", "Rate limit exceeded: maximum inference count (" +
                            std::to_string(max_inferences) +
                            ") reached for this task. The model may be in a loop. "
                            "Please start a new session."},
                {"type", "rate_limit_error"},
                {"code", "max_inferences_per_task_exceeded"},
                {"param", nullptr}
            }}
        };
        http_res.status = 429;
        http_res.set_content(error_resp.dump(), ResponseDispatcher::MIMETYPE_JSON);
        http_res.set_header("Retry-After", "0");
        return false;
    }

    if (max_tokens > 0 && counter.total_tokens > max_tokens)
    {
        My_Log{My_Log::Level::kError}
            << "[GenieRoutingGateway] Token limit exceeded: session=" << session_id
            << ", total_tokens=" << counter.total_tokens
            << " > max_tokens_per_task=" << max_tokens
            << ". Returning error to client." << std::endl;

        json error_resp = {
            {"error", {
                {"message", "Rate limit exceeded: maximum token count (" +
                            std::to_string(max_tokens) +
                            ") reached for this task. "
                            "Please start a new session."},
                {"type", "rate_limit_error"},
                {"code", "max_tokens_per_task_exceeded"},
                {"param", nullptr}
            }}
        };
        http_res.status = 429;
        http_res.set_content(error_resp.dump(), ResponseDispatcher::MIMETYPE_JSON);
        http_res.set_header("Retry-After", "0");
        return false;
    }

    counter.inference_count++;
    My_Log{} << "[GenieRoutingGateway] Rate limit check passed: session=" << session_id
             << ", inference_count=" << counter.inference_count
             << (max_inferences > 0 ? "/" + std::to_string(max_inferences) : "/unlimited")
             << ", prompt_tokens=" << counter.prompt_tokens
             << ", completion_tokens=" << counter.completion_tokens
             << ", total_tokens=" << counter.total_tokens
             << (max_tokens > 0 ? "/" + std::to_string(max_tokens) : "/unlimited")
             << std::endl;
    return true;
}

bool GenieRoutingGateway::UpdateAndCheckTokenCount(const std::string &session_id,
                                                    int64_t tokens_used,
                                                    httplib::Response &/*http_res*/,
                                                    int64_t prompt_tokens,
                                                    int64_t completion_tokens,
                                                    CloudTier cloud_tier)
{

    if (session_id.empty())
        return true;

    if (tokens_used <= 0)
        return true;

    std::lock_guard<std::mutex> lock(rate_limit_mutex_);
    auto &counter = rate_limit_counters_[session_id];
    counter.total_tokens      += tokens_used;
    counter.prompt_tokens     += prompt_tokens;
    counter.completion_tokens += completion_tokens;
    counter.last_updated = std::chrono::steady_clock::now();

    const int max_tokens = (cloud_tier == CloudTier::ENTERPRISE)
        ? enterprise_cloud_config_.rate_limit.max_tokens_per_task
        : cloud_config_.rate_limit.max_tokens_per_task;
    My_Log{} << "[GenieRoutingGateway] Token usage updated: session=" << session_id
             << ", this_call_prompt=" << prompt_tokens
             << ", this_call_completion=" << completion_tokens
             << ", this_call_total=" << tokens_used
             << ", cumulative_prompt=" << counter.prompt_tokens
             << ", cumulative_completion=" << counter.completion_tokens
             << ", cumulative_total=" << counter.total_tokens
             << (max_tokens > 0 ? "/" + std::to_string(max_tokens) : "/unlimited")
             << std::endl;

    return true;
}

bool GenieRoutingGateway::CheckAndIncrementInputOverflowRetry(const std::string &session_id)
{
    const int max_retries = routing_config_.fallback.max_input_overflow_retries;

    if (session_id.empty())
        return true;

    if (max_retries == 0)
    {
        My_Log{My_Log::Level::kWarning}
            << "[GenieRoutingGateway] Input overflow retry disabled (max_input_overflow_retries=0). "
            << "Returning 422 immediately." << std::endl;
        return false;
    }
    if (max_retries < 0)
        return true;

    std::lock_guard<std::mutex> lock(input_overflow_retries_mutex_);

    const int ttl_seconds = (routing_config_.sticky_routing.ttl_seconds > 0)
                            ? routing_config_.sticky_routing.ttl_seconds : 1800;
    auto now_tp = std::chrono::steady_clock::now();
    for (auto it = input_overflow_retries_.begin(); it != input_overflow_retries_.end(); )
    {
        auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(
            now_tp - it->second.last_retry).count();
        if (elapsed > ttl_seconds && it->first != session_id)
            it = input_overflow_retries_.erase(it);
        else
            ++it;
    }

    auto &entry = input_overflow_retries_[session_id];
    entry.last_retry = now_tp;

    if (entry.retry_count >= max_retries)
    {
        My_Log{My_Log::Level::kWarning}
            << "[GenieRoutingGateway] Input overflow retry limit reached: session=" << session_id
            << ", retry_count=" << entry.retry_count
            << " >= max_input_overflow_retries=" << max_retries
            << ". Upgrading 503 to 422." << std::endl;
        return false;
    }

    entry.retry_count++;
    My_Log{} << "[GenieRoutingGateway] Input overflow retry count: session=" << session_id
             << ", retry_count=" << entry.retry_count
             << "/" << max_retries << std::endl;
    return true;
}

void GenieRoutingGateway::ResetInputOverflowRetry(const std::string &session_id)
{
    if (session_id.empty())
        return;

    std::lock_guard<std::mutex> lock(input_overflow_retries_mutex_);
    auto it = input_overflow_retries_.find(session_id);
    if (it != input_overflow_retries_.end())
    {
        My_Log{} << "[GenieRoutingGateway] Input overflow retry count reset for session=" << session_id
                 << ", was=" << it->second.retry_count << std::endl;
        input_overflow_retries_.erase(it);
    }
}

void GenieRoutingGateway::MergeDesensitizationMapping(
        const std::string &session_id,
        const std::unordered_map<std::string, std::string> &mapping)
{
    if (session_id.empty() || mapping.empty()) {
        return;
    }

    std::lock_guard<std::mutex> lock(server_sessions_mutex_);
    auto it = server_sessions_.find(session_id);
    if (it == server_sessions_.end()) {
        My_Log{My_Log::Level::kWarning}
            << "[GenieRoutingGateway] MergeDesensitizationMapping: session_id not found: "
            << session_id << std::endl;
        return;
    }

    for (const auto &[mock, real] : mapping) {
        std::string canonical_real = RestoreStringFully(real, it->second.desensitization_mapping);
        it->second.desensitization_mapping.emplace(mock, canonical_real);
    }

    My_Log{}
        << "[GenieRoutingGateway] Merged " << mapping.size()
        << " desensitization mappings to session " << session_id
        << ", total mappings: " << it->second.desensitization_mapping.size()
        << std::endl;
}

std::unordered_map<std::string, std::string> GenieRoutingGateway::GetDesensitizationMapping(
        const std::string &session_id) const
{
    if (session_id.empty()) {
        return {};
    }

    std::lock_guard<std::mutex> lock(server_sessions_mutex_);
    auto it = server_sessions_.find(session_id);
    if (it == server_sessions_.end()) {
        return {};
    }

    return it->second.desensitization_mapping;
}
