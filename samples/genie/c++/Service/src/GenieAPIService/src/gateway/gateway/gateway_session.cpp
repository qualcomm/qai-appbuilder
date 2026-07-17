//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
//
// genie_routing_gateway_session.cpp
//
// 职责：
//   - 服务端 Session ID 生成与管理（GenerateSessionId / GenerateRequestId /
//     ComputeHistoryFingerprint / ResolveServerSessionIds）
//   - 云端推理限流计数器（CheckAndIncrementInferenceCount /
//     UpdateAndCheckTokenCount / ResetRateLimitCounter）
//   - 本地输入溢出重试计数（CheckAndIncrementInputOverflowRetry /
//     ResetInputOverflowRetry）
//   - 脱敏映射表管理（MergeDesensitizationMapping / GetDesensitizationMapping）
//
//==============================================================================

#include "gateway.h"
#include "log.h"
#include "../../response/response_dispatcher.h"
#include <chrono>
#include <sstream>
#include <iomanip>
#include <random>
#include <mutex>

// ============================================================
// 请求 ID / Session ID 生成
// ============================================================

std::string GenieRoutingGateway::GenerateRequestId()
{
    // 生成简单的请求 ID：时间戳 + 随机数
    auto now = std::chrono::system_clock::now();
    auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        now.time_since_epoch()).count();

    // 使用 thread_local 保证线程安全且高效
    static thread_local std::mt19937 rng(std::random_device{}());
    std::uniform_int_distribution<int> dist(1000, 9999);

    std::ostringstream oss;
    oss << "req_" << ms << "_" << dist(rng);
    return oss.str();
}

std::string GenieRoutingGateway::GenerateSessionId()
{
    // 生成唯一的 session ID：时间戳 + 随机数（格式与 GenerateRequestId 类似）
    auto now = std::chrono::system_clock::now();
    auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        now.time_since_epoch()).count();

    static thread_local std::mt19937 rng(std::random_device{}());
    std::uniform_int_distribution<uint32_t> dist(10000, 99999);

    std::ostringstream oss;
    oss << "sess_" << ms << "_" << dist(rng);
    return oss.str();
}

// ============================================================
// 历史消息前缀哈希（会话指纹）
// ============================================================

std::string GenieRoutingGateway::ComputeHistoryFingerprint(const json &messages)
{
    // 对 messages 数组中除最后一条外的所有非 system/developer 消息的 role+content 计算哈希。
    // 同一会话的连续请求（客户端每次携带完整历史，只追加新消息）会产生相同的哈希。
    // 返回空字符串表示无历史（只有一条或零条非 system 消息）。
    if (!messages.is_array() || messages.empty())
        return "";

    // 收集非 system/developer 消息
    std::vector<const json*> non_system;
    for (const auto &msg : messages)
    {
        const std::string role = msg.value("role", "");
        if (role != "system" && role != "developer")
            non_system.push_back(&msg);
    }

    // 只有一条或零条非 system 消息时，无历史前缀
    if (non_system.size() <= 1)
        return "";

    // 对前 N-1 条非 system 消息的 role+content 计算累积哈希（FNV-1a 变体）
    // 使用简单高效的哈希，不需要密码学强度
    uint64_t hash = 14695981039346656037ULL;  // FNV offset basis
    const uint64_t prime = 1099511628211ULL;  // FNV prime

    auto hash_string = [&](const std::string &s) {
        for (unsigned char c : s) {
            hash ^= c;
            hash *= prime;
        }
        // 分隔符，防止 "ab"+"c" 与 "a"+"bc" 产生相同哈希
        hash ^= 0xFF;
        hash *= prime;
    };

    for (size_t i = 0; i < non_system.size() - 1; ++i)
    {
        const auto &msg = *non_system[i];
        hash_string(msg.value("role", ""));

        // content 可能是字符串或数组（多模态）
        if (msg.contains("content"))
        {
            if (msg["content"].is_string())
            {
                hash_string(msg["content"].get<std::string>());
            }
            else
            {
                // 非字符串 content：序列化为 JSON 字符串参与哈希
                hash_string(msg["content"].dump());
            }
        }
    }

    // 转换为十六进制字符串
    std::ostringstream oss;
    oss << std::hex << std::setfill('0') << std::setw(16) << hash;
    return oss.str();
}

// 辅助：计算第一条 user 消息的哈希（first_msg_key），用于跨轮次 session 查找
// 格式：前缀 "fmk_" + 16位十六进制哈希
// 返回空字符串表示第一条非 system 消息不是 user 消息
static std::string ComputeFirstMsgKey(const std::vector<const json*> &non_system)
{
    if (non_system.empty() || non_system[0]->value("role", "") != "user")
        return "";

    uint64_t h = 14695981039346656037ULL;
    const uint64_t p = 1099511628211ULL;

    // 哈希 role
    for (unsigned char c : std::string("user")) { h ^= c; h *= p; }
    h ^= 0xFF; h *= p;

    // 哈希 content
    std::string content_str;
    if (non_system[0]->contains("content") && (*non_system[0])["content"].is_string())
        content_str = (*non_system[0])["content"].get<std::string>();
    else if (non_system[0]->contains("content"))
        content_str = (*non_system[0])["content"].dump();
    for (unsigned char c : content_str) { h ^= c; h *= p; }
    h ^= 0xFF; h *= p;

    std::ostringstream oss;
    oss << "fmk_" << std::hex << std::setfill('0') << std::setw(16) << h;
    return oss.str();
}

// 辅助：将同一个 session entry 注册到多个 key 下（自动跳过空 key），
// 用于消除 ResolveServerSessionIds 中 5 处重复的 "if (!key.empty()) map[key] = entry;" 样板代码
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

// ============================================================
// 服务端 Session ID 解析与分配
// ============================================================

void GenieRoutingGateway::ResolveServerSessionIds(const json &request, InspectionContext &ctx)
{
    // 从 request["messages"] 中提取消息列表
    const json &messages = request.contains("messages") && request["messages"].is_array()
                           ? request["messages"]
                           : json::array();

    // ── 1. 收集非 system/developer 消息 ──────────────────────────────────────
    std::vector<const json*> non_system;
    for (const auto &msg : messages)
    {
        const std::string role = msg.value("role", "");
        if (role != "system" && role != "developer")
            non_system.push_back(&msg);
    }

    // ── 2. 判断是否为新 global session ────────────────────────────────────────
    // 条件：过滤 system/developer 后，只有1条 user 消息，且无 tool/assistant 消息
    // 这意味着客户端清空了历史，开始了全新的会话
    bool is_new_global_session = (non_system.size() == 1 &&
                                   non_system[0]->value("role", "") == "user");

    // ── 3. 判断是否为新 user 轮次 ─────────────────────────────────────────────
    // 条件：最后一条非 system/developer 消息是 user 消息
    // 这意味着用户发起了新的请求（而非工具调用返回）
    bool is_new_user_turn = (!non_system.empty() &&
                              non_system.back()->value("role", "") == "user");

    // ── 4. 计算历史消息前缀哈希（会话指纹）和第一条消息 key ──────────────────
    std::string fingerprint   = ComputeHistoryFingerprint(messages);
    std::string first_msg_key = ComputeFirstMsgKey(non_system);

    // ── 5. 查找或创建 session IDs ─────────────────────────────────────────────
    std::lock_guard<std::mutex> lock(server_sessions_mutex_);

    // TTL 清理：使用 sticky_routing.ttl_seconds 作为 server_sessions_ 的 TTL
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
        // 新会话：生成全新的 global_session_id 和 user_session_id
        // 旧的 global_session_id 会通过 TTL 自然过期，无需立即清理 session_security_states_
        ServerSessionEntry entry;
        entry.global_session_id = GenerateSessionId();
        entry.user_session_id   = GenerateSessionId();
        entry.last_updated      = now_tp;

        // 新会话时 fingerprint 为空（只有1条 user 消息，无历史前缀）。
        // 用 first_msg_key（第一条 user 消息内容哈希）缓存 session，
        // 使后续工具调用轮次（第2轮起）能通过 first_msg_key 找到此 session。
        //
        // 机制：
        //   第1轮：messages=[user_A]，fingerprint=""，first_msg_key=fmk_hash(user_A)
        //          → server_sessions_[first_msg_key] = entry
        //   第2轮：messages=[user_A, assistant_B, tool_C]，fingerprint=hash(user_A+assistant_B)
        //          → fingerprint 未命中 → 通过 first_msg_key 找到 entry
        //          → 将 fingerprint 也注册到 server_sessions_，加速后续查找
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
        // 无历史前缀（消息列表为空或只有一条非 system 消息但不是 user）
        // 生成临时 session IDs。
        // 即使是临时 session，也需要以 user_session_id 为 key 存储，
        // 否则后续 MergeDesensitizationMapping/GetDesensitizationMapping 无法找到映射表。
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
        // 有历史前缀：先通过 fingerprint 查找，再通过 first_msg_key 查找
        auto it = server_sessions_.find(fingerprint);
        if (it == server_sessions_.end() && !first_msg_key.empty())
            it = server_sessions_.find(first_msg_key);

        if (it != server_sessions_.end())
        {
            // 找到已有条目（通过 fingerprint 或 first_msg_key）
            // 先拷贝 entry，避免后续 map 插入导致迭代器失效
            ServerSessionEntry found_entry = it->second;
            found_entry.last_updated = now_tp;

            if (is_new_user_turn)
            {
                // 新 user 轮次：生成新的 user_session_id，沿用 global_session_id
                // 新的 user_session_id 会使旧的 sticky_routing 和 rate_limit 计数器自然失效
                std::string old_uid = found_entry.user_session_id;
                found_entry.user_session_id = GenerateSessionId();

                // 同步更新 fingerprint、first_msg_key、user_session_id 三个条目
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
                // 工具调用轮次：沿用两个 session IDs，同步注册 fingerprint、first_msg_key、user_session_id
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
            // 真正的新会话（服务重启后第一次请求，或 TTL 过期后的新请求）
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

// ============================================================
// 云端推理限流：检查并递增推理次数计数器
// ============================================================
bool GenieRoutingGateway::CheckAndIncrementInferenceCount(const std::string &session_id,
                                                           httplib::Response &http_res,
                                                           CloudTier cloud_tier)
{
    // 根据 cloud_tier 选择对应的 rate_limit 配置
    const int max_inferences = (cloud_tier == CloudTier::ENTERPRISE)
        ? enterprise_cloud_config_.rate_limit.max_inferences_per_task
        : cloud_config_.rate_limit.max_inferences_per_task;
    const int max_tokens = (cloud_tier == CloudTier::ENTERPRISE)
        ? enterprise_cloud_config_.rate_limit.max_tokens_per_task
        : cloud_config_.rate_limit.max_tokens_per_task;

    // session_id 为空时跳过限流（无 session 场景）
    if (session_id.empty())
        return true;

    std::lock_guard<std::mutex> lock(rate_limit_mutex_);

    // ── 定期清理过期的 counter（防止内存无限增长）──────────────────────────
    // 使用 sticky_routing.ttl_seconds 作为 counter 的 TTL（与 sticky session 保持一致）。
    // 每次推理请求时顺便清理，避免额外的定时器线程。
    {
        const int ttl_seconds = (routing_config_.sticky_routing.ttl_seconds > 0)
                                ? routing_config_.sticky_routing.ttl_seconds
                                : 3600;  // 默认 1 小时
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

    // ── 检查推理次数限制 ──────────────────────────────────────────────────
    if (max_inferences > 0 && counter.inference_count >= max_inferences)
    {
        My_Log{My_Log::Level::kError}
            << "[GenieRoutingGateway] Rate limit exceeded: session=" << session_id
            << ", inference_count=" << counter.inference_count
            << " >= max_inferences_per_task=" << max_inferences
            << ". Returning error to client." << std::endl;

        // 构建错误响应（429 Too Many Requests）
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

    // ── 检查 Token 数限制 ──────────────────────────────────────────────────
    // 在每次推理前检查上一次推理后的累计 Token 数是否已超限
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

// ============================================================
// 云端推理限流：更新并检查 Token 消耗计数
// ============================================================
bool GenieRoutingGateway::UpdateAndCheckTokenCount(const std::string &session_id,
                                                    int64_t tokens_used,
                                                    httplib::Response &/*http_res*/,
                                                    int64_t prompt_tokens,
                                                    int64_t completion_tokens,
                                                    CloudTier cloud_tier)
{
    // session_id 为空时跳过
    if (session_id.empty())
        return true;

    // tokens_used <= 0 时跳过（无有效 token 数据）
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

    // 注意：此处不返回错误响应，Token 超限检查在下一次 CheckAndIncrementInferenceCount 时触发
    return true;
}

// ============================================================
// 云端推理限流：重置指定 session 的计数器
// ============================================================
void GenieRoutingGateway::ResetRateLimitCounter(const std::string &session_id)
{
    if (session_id.empty())
        return;

    std::lock_guard<std::mutex> lock(rate_limit_mutex_);
    auto it = rate_limit_counters_.find(session_id);
    if (it != rate_limit_counters_.end())
    {
        My_Log{} << "[GenieRoutingGateway] Rate limit counter reset for session=" << session_id
                 << ", inference_count=" << it->second.inference_count
                 << ", prompt_tokens=" << it->second.prompt_tokens
                 << ", completion_tokens=" << it->second.completion_tokens
                 << ", total_tokens=" << it->second.total_tokens << std::endl;
        rate_limit_counters_.erase(it);
    }
}

// ============================================================
// 本地输入溢出重试计数：检查并递增
// ============================================================
bool GenieRoutingGateway::CheckAndIncrementInputOverflowRetry(const std::string &session_id)
{
    const int max_retries = routing_config_.fallback.max_input_overflow_retries;

    // session_id 为空时，始终允许（返回 true）
    if (session_id.empty())
        return true;

    // max_retries == 0：禁止任何重试，直接返回 422（永久失败）
    // max_retries < 0：不限制重试次数，始终允许（返回 true）
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

    // 延迟清理：TTL 使用 sticky_routing.ttl_seconds（默认 30 分钟）
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
        return false;  // 已超限，应返回 422
    }

    entry.retry_count++;
    My_Log{} << "[GenieRoutingGateway] Input overflow retry count: session=" << session_id
             << ", retry_count=" << entry.retry_count
             << "/" << max_retries << std::endl;
    return true;  // 未超限，可返回 503
}

// ============================================================
// 本地输入溢出重试计数：重置（成功路由到云端后调用）
// ============================================================
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

// ============================================================
// 脱敏映射表管理
// ============================================================

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

    // 合并映射表（不覆盖已有 key，保留最早轮次的真实值）
    // 迭代脱敏场景下，第1轮的 mock_user_1 → friend@example.com 是正确的真实值，
    // 不应被第2轮的 mock_user_1 → 13890000001@163.com（中间脱敏产物）覆盖。
    for (const auto &[mock, real] : mapping) {
        std::string canonical_real = RestoreStringFully(real, it->second.desensitization_mapping);
        it->second.desensitization_mapping.emplace(mock, canonical_real);  // emplace 不覆盖已有 key
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
