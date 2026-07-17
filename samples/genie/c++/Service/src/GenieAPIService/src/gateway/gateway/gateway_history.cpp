//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
//
// genie_routing_gateway_history.cpp
//
// 职责：
//   - 本地历史清洗（CleanLocalHistoryForCloudFallback）
//   - S2 轮次历史清洗（PurgeS2TurnsFromHistory / CleanS2TurnsForCloud）
//
//==============================================================================

#include "gateway.h"
#include "log.h"
#include <set>
#include <mutex>

// ============================================================
// 清理本地历史记录（事后路由回退时使用）
// 将 messages 中本地模型产生的中间 tool_calls/tool 消息剔除，
// 只保留最后一条 user 消息及之前的对话历史，以及不含 tool_calls 的 assistant 文本回复。
//
// fallback_boundary 参数说明：
//   = 0（默认）：第一次 fallback，清理 last_user_idx 之后所有 tool/tool_calls 消息
//   > 0：sticky session 命中，只清理 last_user_idx 之后且索引 < fallback_boundary 的本地历史，
//        保留索引 >= fallback_boundary 的云端历史（云端模型已完成的工具调用不应被清除）
//
// 背景：客户端每次请求都携带完整历史（包括本地模型产生的工具调用记录），
// GenieAPIService 对历史的清理不影响客户端自己保存的记录。
// 因此在 sticky session 命中时，last_user_idx 之后混合了本地历史和云端历史，
// 必须通过 fallback_boundary 区分两者，只清理本地部分。
// ============================================================
json GenieRoutingGateway::CleanLocalHistoryForCloudFallback(const json &request, int fallback_boundary)
{
    json cleaned = request;
    if (!cleaned.contains("messages") || !cleaned["messages"].is_array()) return cleaned;

    const auto &messages = cleaned["messages"];

    // 1. 找到最后一条 role=user 的位置
    int last_user_idx = -1;
    for (int i = (int)messages.size() - 1; i >= 0; --i)
    {
        if (messages[i].value("role", "") == "user")
        {
            last_user_idx = i;
            break;
        }
    }
    if (last_user_idx < 0) return cleaned; // 没有 user 消息，不清理

    // 2. 保留 last_user_idx 及之前的所有消息
    //    （包括 system、user、assistant 正常回复，但不包括 last_user_idx 之后的 tool_calls/tool）
    json new_messages = json::array();
    for (int i = 0; i <= last_user_idx; ++i)
    {
        new_messages.push_back(messages[i]);
    }

    // 3. last_user_idx 之后：根据 fallback_boundary 区分本地历史和云端历史
    //    - fallback_boundary == 0（第一次 fallback）：清理所有 tool/tool_calls 消息
    //    - fallback_boundary > 0（sticky session）：
    //        索引 < fallback_boundary：本地历史，清理 tool/tool_calls
    //        索引 >= fallback_boundary：云端历史，全部保留
    for (int i = last_user_idx + 1; i < (int)messages.size(); ++i)
    {
        const auto &msg = messages[i];

        // sticky session 场景：索引 >= fallback_boundary 的消息是云端历史，直接保留
        if (fallback_boundary > 0 && i >= fallback_boundary)
        {
            new_messages.push_back(msg);
            continue;
        }

        std::string role = msg.value("role", "");
        if (role == "tool") continue;                                    // 剔除本地 tool 结果
        if (role == "assistant" && msg.contains("tool_calls")) continue; // 剔除本地含 tool_calls 的 assistant
        new_messages.push_back(msg);                                     // 保留正常 assistant 文本回复
    }

    cleaned["messages"] = new_messages;

    My_Log{} << "[GenieRoutingGateway] CleanLocalHistoryForCloudFallback: "
             << "original_messages=" << messages.size()
             << ", cleaned_messages=" << new_messages.size()
             << ", fallback_boundary=" << fallback_boundary << std::endl;

    return cleaned;
}

// ============================================================
// [S2 轮次清理] PurgeS2TurnsFromHistory：从历史消息中清洗 S2 轮次
// ============================================================
// 算法：
//   1. 收集所有 S2 轮次范围内的消息索引（待删除集合）
//   2. 重建 messages 数组，跳过待删除的消息
//   3. 在每个被删除轮次的位置插入一对 user+assistant 占位消息，保持上下文连贯性
//
// 注意：
//   - 占位消息使用一对 role=user + role=assistant（而非单条 role=assistant），原因：
//     S2 轮次的第一条消息是 role=user，若只插入 role=assistant，会导致对话历史中
//     出现 system/user/assistant 之后直接跟 assistant 的格式违规（OpenAI/Claude API
//     要求 user 和 assistant 消息必须交替出现）。
//     插入一对 user+assistant 占位消息可保持对话格式合规，同时语义上表示
//     "该轮用户提问和助手回复均因安全策略被移除"。
//   - 每个连续的 S2 轮次范围只插入一对占位消息（避免多对占位消息堆叠）
//   - 若 s2_turn_ranges 为空，直接返回原始请求副本
//
// [边缘情况说明] 占位消息数量与被删除消息数量的差异：
//   当 S2 轮次只有 1 条消息（如 user 消息被拒绝后没有 assistant 回复）时，
//   删除 1 条但插入 2 条占位消息，导致清洗后消息数 > 清洗前消息数。
//   CleanS2TurnsForCloud 将 checked_message_count 设为原始消息数（request["messages"].size()），
//   而非清洗后的消息数，因此此边缘情况不影响 checked_message_count 的正确性。
//   下一轮客户端请求若添加了至少 1 条新消息（正常情况），消息数仍 >= checked_message_count，
//   历史篡改检测通过，不影响功能。
// ============================================================
json GenieRoutingGateway::PurgeS2TurnsFromHistory(
    const json &request,
    const std::vector<SessionSecurityState::S2TurnRange> &s2_turn_ranges,
    bool log_details)
{
    if (s2_turn_ranges.empty()) return request;
    if (!request.contains("messages") || !request["messages"].is_array()) return request;

    const auto &messages = request["messages"];
    int total = (int)messages.size();

    // 1. 构建待删除索引集合（所有 S2 轮次范围内的消息索引）
    // 排除 system/developer 消息：
    //   当 turn_start_idx == -1（messages 中没有 user 消息）时，UpdateSessionSecurityState
    //   使用兜底值 effective_start=0，导致 S2 轮次范围从索引 0 开始。
    //   若不排除 system/developer 消息，PurgeS2TurnsFromHistory 会将其纳入删除集合，
    //   并在索引 0 处插入占位消息对，导致 system 消息被推到占位消息之后，
    //   违反 OpenAI/Claude API 要求（system 消息必须在最前面），云端 API 会返回格式错误。
    //   修复：构建 indices_to_remove 时跳过 role=system/developer 的消息，
    //   确保 system/developer 消息始终保留在消息数组头部，不参与 S2 轮次清洗。
    std::set<int> indices_to_remove;
    for (const auto &range : s2_turn_ranges) {
        int start = std::max(0, range.start_idx);
        int end = std::min(total, range.end_idx);
        for (int i = start; i < end; ++i) {
            const std::string role = messages[i].value("role", "");
            if (role != "system" && role != "developer") {
                indices_to_remove.insert(i);
            }
        }
    }

    if (indices_to_remove.empty()) return request;

    // 2. 重建 messages 数组，跳过待删除消息，并在每个连续删除段插入占位消息对
    json new_messages = json::array();
    bool in_removed_segment = false;

    for (int i = 0; i < total; ++i) {
        bool is_removed = (indices_to_remove.count(i) > 0);

        if (is_removed) {
            // 进入删除段：若尚未插入占位消息，则插入一对 user+assistant 占位消息
            if (!in_removed_segment) {
                // 插入 user 占位消息（代替被删除的 user 消息，保持对话格式合规）
                // OpenAI/Claude API 要求 user 和 assistant 消息必须交替出现，
                // 若只插入 assistant 占位，会导致连续两条 assistant 消息的格式违规。
                json user_placeholder = json::object();
                user_placeholder["role"] = "user";
                user_placeholder["content"] = "[Previous user message was removed due to security policy]";
                new_messages.push_back(user_placeholder);

                // 插入 assistant 占位消息（代替被删除的 assistant 回复）
                json assistant_placeholder = json::object();
                assistant_placeholder["role"] = "assistant";
                assistant_placeholder["content"] = "[Previous assistant response was removed due to security policy]";
                new_messages.push_back(assistant_placeholder);

                in_removed_segment = true;
            }
            // 跳过该消息（不加入 new_messages）
        } else {
            // 离开删除段
            in_removed_segment = false;
            new_messages.push_back(messages[i]);
        }
    }

    json result = request;
    result["messages"] = new_messages;

    if (log_details) {
        My_Log{} << "[GenieRoutingGateway] PurgeS2TurnsFromHistory: "
                 << "original_messages=" << total
                 << ", removed=" << indices_to_remove.size()
                 << ", cleaned_messages=" << new_messages.size()
                 << ", s2_turn_ranges=" << s2_turn_ranges.size() << std::endl;
        for (size_t i = 0; i < s2_turn_ranges.size(); ++i) {
            My_Log{} << "[GenieRoutingGateway]   S2 turn[" << i << "]: "
                     << "[" << s2_turn_ranges[i].start_idx << ", " << s2_turn_ranges[i].end_idx << ")"
                     << std::endl;
        }
    } else {
        My_Log{} << "[GenieRoutingGateway] PurgeS2TurnsFromHistory: "
                 << "removed " << indices_to_remove.size() << " messages from "
                 << s2_turn_ranges.size() << " S2 turn(s), "
                 << "cleaned_messages=" << new_messages.size() << std::endl;
    }

    return result;
}

// ============================================================
// [S2 轮次清理] CleanS2TurnsForCloud：从 session 状态读取 S2 轮次范围，
// 修正 end_idx，执行清洗，并重置 session 状态。
// 被 cloud-only 路径（S0/S1 分支）和 Routing 路径（Step 3.5）共同调用。
// ============================================================
json GenieRoutingGateway::CleanS2TurnsForCloud(
    const json &request,
    const std::string &global_session_id,
    int current_turn_start,
    SensitivityLevel new_max_sensitivity,
    const std::string &log_tag)
{
    if (!routing_config_.s2_turn_cleaning.enabled || global_session_id.empty()) {
        return request;
    }

    // 1. 读取 S2 轮次范围（加锁）
    std::vector<SessionSecurityState::S2TurnRange> s2_ranges;
    {
        std::lock_guard<std::mutex> lock(session_security_mutex_);
        auto it = session_security_states_.find(global_session_id);
        if (it == session_security_states_.end() || it->second.s2_turn_ranges.empty()) {
            return request;  // 无 S2 轮次记录，直接返回原始请求
        }
        s2_ranges = it->second.s2_turn_ranges;
    }

    // 2. 修正所有 S2 轮次的 end_idx，确保每个轮次的范围精确覆盖到下一轮起始位置。
    // 原实现只修正最后一个 S2 轮次的 end_idx，导致前面的 S2 轮次
    // 的 end_idx 保持 UpdateSessionSecurityState 中记录的原始值（即该轮检测时的消息总数），
    // 可能导致 S2 轮次之间的 assistant 回复消息残留在清洗后的历史中，造成上下文不连贯。
    // 修复：按顺序将每个 S2 轮次的 end_idx 修正为下一个 S2 轮次的 start_idx，
    // 最后一个 S2 轮次的 end_idx 修正为 current_turn_start（当前轮起始位置）。
    for (size_t i = 0; i < s2_ranges.size(); ++i) {
        if (i + 1 < s2_ranges.size()) {
            // 非最后一个 S2 轮次：end_idx 修正为下一个 S2 轮次的 start_idx
            // 这样可以覆盖两个 S2 轮次之间的所有消息（包括 assistant 回复等）
            s2_ranges[i].end_idx = s2_ranges[i + 1].start_idx;
        } else {
            // 最后一个 S2 轮次：end_idx 修正为当前轮起始位置（包含上一轮 assistant 回复）
            if (current_turn_start >= 0) {
                s2_ranges[i].end_idx = current_turn_start;
            }
        }
    }

    // 3. 执行清洗
    json cleaned = PurgeS2TurnsFromHistory(request, s2_ranges,
                                           routing_config_.s2_turn_cleaning.log_details);

    // 4. 重置 session S2 状态（清洗后历史中不再含 S2 内容）
    {
        std::lock_guard<std::mutex> lock(session_security_mutex_);
        auto it = session_security_states_.find(global_session_id);
        if (it != session_security_states_.end()) {
            it->second.s2_turn_ranges.clear();
            it->second.max_sensitivity_seen = new_max_sensitivity;
            it->second.incremental_mode_enabled = true;
            it->second.fallback_reason = "";
            // 将 checked_message_count 更新为原始请求的消息数量，
            // 而非清洗后的消息数量。
            // 背景：客户端不知道服务端做了 S2 历史清洗，下一轮仍会发来完整的原始历史（N+新消息条）。
            // 若将 checked_message_count 设为清洗后的小值（如 46），下一轮增量检查会把
            // N-46 条原始历史当作"新消息"重新检测，产生错误的 S2 turn range，
            // 导致后续清洗范围错误，二次检查仍发现 S2，触发安全兜底强制走 LOCAL，脱敏失败。
            // 修复：设为原始消息数（request["messages"].size()），下一轮增量检查只检查
            // 真正的新消息（N+新消息 - N = 新消息条数），行为正确。
            // 安全性：若下一轮客户端发来的消息数少于原始消息数（客户端删除了历史），
            // DetermineCheckScope 的历史篡改检测会触发全量检查，安全红线不变。
            if (request.contains("messages") && request["messages"].is_array()) {
                it->second.checked_message_count = (int)request["messages"].size();
            }
        }
    }

    My_Log{} << "[GenieRoutingGateway] " << log_tag << ": cleaned "
             << s2_ranges.size() << " S2 turn(s) from history"
             << ", new_max_sensitivity=" << to_string(new_max_sensitivity)
             << std::endl;

    return cleaned;
}
