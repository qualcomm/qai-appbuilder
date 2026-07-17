//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
//
// genie_routing_gateway_incremental.cpp
//
// 职责：
//   - DetermineCheckScope：决定本次请求的安全检查范围（全量 or 增量）
//   - ExtractIncrementalContent：提取需要检查的增量内容
//   - DetectSensitiveReference：检测新消息中是否引用历史敏感信息
//   - UpdateSessionSecurityState：更新会话安全状态
//
//==============================================================================

#include "gateway.h"
#include "log.h"
#include <algorithm>
#include <mutex>

// ============================================================
// DetermineCheckScope：决定本次请求的安全检查范围
// ============================================================
CheckScope GenieRoutingGateway::DetermineCheckScope(
    const json &request,
    const std::string &global_session_id,
    int &out_last_checked_count)
{
    out_last_checked_count = 0;

    // 1. 功能开关：未启用时始终全量检查
    if (!routing_config_.incremental_check.enabled) {
        return CheckScope::FULL;
    }

    // 2. global_session_id 为空 → 全量检查（无状态可用）
    if (global_session_id.empty()) {
        return CheckScope::FULL;
    }

    // 3. 延迟清理策略：只清理当前查询的 session（如果过期）
    //    其他过期条目延迟到下次访问时清理，避免遍历所有会话导致锁持有时间过长
    std::lock_guard<std::mutex> lock(session_security_mutex_);

    auto it = session_security_states_.find(global_session_id);

    // 4. 首次请求 → 全量检查（建立基线）
    if (it == session_security_states_.end()) {
        return CheckScope::FULL;
    }

    auto &state = it->second;

    // 5. TTL 过期 → 全量检查（状态失效）
    //    延迟清理：只清理当前 session，不遍历其他会话
    auto now = std::chrono::steady_clock::now();
    auto age = std::chrono::duration_cast<std::chrono::seconds>(
        now - state.last_updated).count();
    if (age > routing_config_.incremental_check.session_ttl_seconds) {
        session_security_states_.erase(it);
        return CheckScope::FULL;
    }

    // 6. 增量模式被禁用 → 全量检查（之前检测到风险或历史篡改）
    // 例外：若 s2_turn_cleaning 已启用且有 S2 轮次记录，
    // 允许跳过此检查，继续到第8步的 S2 红线逻辑（允许增量检查当前轮）。
    // 背景：S2 检测到时 incremental_mode_enabled 被设为 false，但 s2_turn_cleaning
    // 需要对当前轮做增量检查（只检查新消息），以判断当前轮是否含 S2。
    //
    // 增加 fallback_reason == "s2_detected" 条件：
    // 若 incremental_mode_enabled=false 是因为 sensitive_reference_detected 等其他原因，
    // 即使 s2_turn_ranges 非空，也不应绕过全量检查要求。
    // 只有当禁用原因明确是 s2_detected 时，才允许 S2 轮次清理例外生效。
    if (!state.incremental_mode_enabled) {
        // S2 轮次清理例外：仅当禁用原因是 s2_detected 时才允许继续到第8步
        if (routing_config_.s2_turn_cleaning.enabled &&
            !state.s2_turn_ranges.empty() &&
            state.max_sensitivity_seen == SensitivityLevel::S2 &&
            state.fallback_reason == "s2_detected") {
            // 不返回 FULL，继续执行后续检查（第7步、第8步）
        } else {
            return CheckScope::FULL;
        }
    }

    // 7. 检测历史篡改：消息数量异常减少（客户端删除历史消息）
    int current_msg_count = request.contains("messages") && request["messages"].is_array()
                            ? (int)request["messages"].size() : 0;
    if (routing_config_.incremental_check.detect_history_tampering &&
        current_msg_count < state.checked_message_count) {
        state.incremental_mode_enabled = false;
        state.fallback_reason = "message_count_decreased";
        My_Log{My_Log::Level::kWarning}
            << "[GenieRoutingGateway] History tampering detected: "
            << "current=" << current_msg_count
            << " < checked=" << state.checked_message_count
            << ". Falling back to full check." << std::endl;
        return CheckScope::FULL;
    }

    // 注意：可选的哈希校验（检测消息内容修改）可在此处添加
    // 但开销较大，本期不实施（见设计文档 5.1 节风险分析）

    // 8. S2 红线：历史中曾出现 S2
    if (routing_config_.incremental_check.s2_always_full_check &&
        state.max_sensitivity_seen == SensitivityLevel::S2) {
        // [S2 轮次清理] 若 s2_turn_cleaning 已启用且有 S2 轮次记录，
        // 允许对当前轮新消息做增量检查（只检查当前轮，不扫描历史 S2 内容）。
        // 若当前轮无 S2，HandleChatCompletion 会清洗历史中的 S2 轮次后再上云。
        if (routing_config_.s2_turn_cleaning.enabled &&
            !state.s2_turn_ranges.empty()) {
            out_last_checked_count = state.checked_message_count;
            My_Log{} << "[GenieRoutingGateway] S2 history with turn ranges: "
                     << "allowing incremental check for current turn "
                     << "(s2_turns=" << state.s2_turn_ranges.size() << ")" << std::endl;
            return CheckScope::INCREMENTAL;
        }
        // 无 S2 轮次记录（如 s2_turn_cleaning 未启用，或旧版本状态）→ 全量检查
        return CheckScope::FULL;
    }

    // 9. 无新增消息 → 全量检查（防止边界情况）
    if (current_msg_count <= state.checked_message_count) {
        return CheckScope::FULL;
    }

    // 10. 通过所有检查 → 增量检查
    out_last_checked_count = state.checked_message_count;
    return CheckScope::INCREMENTAL;
}

// ============================================================
// ExtractIncrementalContent：提取需要检查的增量内容
// ============================================================
json GenieRoutingGateway::ExtractIncrementalContent(
    const json &request,
    int last_checked_count)
{
    json incremental = json::object();
    incremental["messages"] = json::array();

    if (!request.contains("messages") || !request["messages"].is_array()) {
        return incremental;
    }

    const auto &messages = request["messages"];

    // 提取 last_checked_count 之后的新增消息
    // 包括：新的 user 消息、assistant 消息（含 tool_calls）、tool 输出消息
    for (int i = last_checked_count; i < (int)messages.size(); ++i) {
        incremental["messages"].push_back(messages[i]);
    }

    // 注意：不包含 request["tools"]（工具定义/schema）
    // 理由：工具定义在同一会话中通常不变，且不包含用户隐私数据

    return incremental;
}

// ============================================================
// DetectSensitiveReference：检测新消息中是否引用历史敏感信息
// ============================================================
bool GenieRoutingGateway::DetectSensitiveReference(const json &incremental_messages)
{
    // 检测引用模式（中英文）
    static const std::vector<std::string> reference_patterns = {
        "刚才的", "之前的", "上面的", "前面提到的", "上次的",
        "the previous", "the above", "mentioned earlier", "as above"
    };

    static const std::vector<std::string> sensitive_keywords = {
        "手机", "电话", "邮箱", "身份证", "银行卡", "密码", "卡号",
        "phone", "email", "id card", "bank card", "password", "card number"
    };

    if (!incremental_messages.is_array()) return false;

    for (const auto &msg : incremental_messages) {
        std::string text;
        if (msg.contains("content") && msg["content"].is_string()) {
            text = msg["content"].get<std::string>();
        } else {
            continue;
        }

        // 转小写（用于英文匹配）
        std::string lower_text = text;
        std::transform(lower_text.begin(), lower_text.end(), lower_text.begin(),
                       [](unsigned char c) { return std::tolower(c); });

        for (const auto &ref : reference_patterns) {
            bool ref_found = (text.find(ref) != std::string::npos) ||
                             (lower_text.find(ref) != std::string::npos);
            if (ref_found) {
                for (const auto &kw : sensitive_keywords) {
                    if (text.find(kw) != std::string::npos ||
                        lower_text.find(kw) != std::string::npos) {
                        return true;  // 检测到引用模式
                    }
                }
            }
        }
    }
    return false;
}

// ============================================================
// UpdateSessionSecurityState：更新会话安全状态
// ============================================================
void GenieRoutingGateway::UpdateSessionSecurityState(
    const std::string &global_session_id,
    const InspectionResult &inspection,
    int current_message_count,
    int turn_start_idx)
{
    if (global_session_id.empty()) return;

    std::lock_guard<std::mutex> lock(session_security_mutex_);

    // max_sessions 防护（与 sticky_sessions_ 保持一致）
    if ((int)session_security_states_.size() >= routing_config_.incremental_check.max_sessions) {
        // LRU 淘汰：unordered_map 的 begin() 不保证插入顺序，
        // 改为遍历找到 last_updated 最早的条目进行淘汰，避免随机删除活跃 session。
        auto oldest_it = session_security_states_.begin();
        for (auto it = session_security_states_.begin(); it != session_security_states_.end(); ++it) {
            if (it->second.last_updated < oldest_it->second.last_updated) {
                oldest_it = it;
            }
        }
        My_Log{} << "[GenieRoutingGateway] session_security_states at max capacity, removed LRU entry: "
                 << oldest_it->first << std::endl;
        session_security_states_.erase(oldest_it);
    }

    auto &state = session_security_states_[global_session_id];

    // 更新最高敏感等级（通常只升不降）
    if (static_cast<int>(inspection.sensitivity_level) >
        static_cast<int>(state.max_sensitivity_seen)) {
        state.max_sensitivity_seen = inspection.sensitivity_level;

        // S2 命中：永久禁用增量模式（不可恢复）
        if (inspection.sensitivity_level == SensitivityLevel::S2) {
            state.incremental_mode_enabled = false;
            state.fallback_reason = "s2_detected";
            My_Log{} << "[GenieRoutingGateway] S2 detected, incremental mode permanently disabled for global_session="
                     << global_session_id << std::endl;
        }
    }
    // 注意：max_sensitivity_seen 从 S2 降级的逻辑由 CleanS2TurnsForCloud 负责，
    // 该函数在清洗 S2 轮次后会将 max_sensitivity_seen 重置为 new_max_sensitivity 参数值。
    // 此处无需额外的降级逻辑：UpdateSessionSecurityState 被调用时 s2_turn_ranges 始终非空
    // （CleanS2TurnsForCloud 总是在 UpdateSessionSecurityState 之后执行），
    // 因此 s2_turn_ranges.empty() 条件在此处永远不满足。

    // 若本轮检测到 S2，则记录该轮次范围
    // 原实现要求 turn_start_idx >= 0 才记录，但当 messages 中没有 user 消息时
    // turn_start_idx == -1，导致 S2 轮次范围不被记录，s2_turn_ranges 永远为空，
    // S2 轮次清理功能对这类请求完全静默失效，退化为旧行为（后续轮次全量检查并报 S2）。
    // 修复：当 turn_start_idx == -1 时，使用 0 作为兜底起始索引（覆盖整个历史）。
    if (routing_config_.s2_turn_cleaning.enabled &&
        inspection.sensitivity_level == SensitivityLevel::S2)
    {
        // turn_start_idx == -1 时（messages 中没有 user 消息），使用 0 作为兜底起始索引
        int effective_start = (turn_start_idx >= 0) ? turn_start_idx : 0;

        // 检查是否已有相同起始索引的范围（避免重复记录）
        bool already_recorded = false;
        for (const auto &r : state.s2_turn_ranges) {
            if (r.start_idx == effective_start) {
                already_recorded = true;
                break;
            }
        }
        if (!already_recorded) {
            SessionSecurityState::S2TurnRange range;
            range.start_idx = effective_start;
            range.end_idx = current_message_count;  // 当前轮次结束位置（不含）
            state.s2_turn_ranges.push_back(range);
            if (routing_config_.s2_turn_cleaning.log_details) {
                My_Log{} << "[GenieRoutingGateway] S2 turn range recorded: "
                         << "global_session=" << global_session_id
                         << ", turn=[" << range.start_idx << ", " << range.end_idx << ")"
                         << (turn_start_idx < 0 ? " (fallback start=0, no user msg found)" : "")
                         << ", total_s2_turns=" << state.s2_turn_ranges.size() << std::endl;
            }
        }
    }

    // 更新已检查消息数量
    state.checked_message_count = current_message_count;
    state.last_updated = std::chrono::steady_clock::now();
}
