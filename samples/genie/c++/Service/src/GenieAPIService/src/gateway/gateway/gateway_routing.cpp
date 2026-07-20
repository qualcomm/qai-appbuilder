//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
//
// gateway_routing.cpp
//
// 职责：
//   - HandleChatCompletion：主路由入口，编排 Step1~Step6
//     - 服务端 session ID 生成
//     - 用户前缀关键字强制路由（/cloud、/local、/企业云 等）
//     - sensitivity_detection.enabled 总开关
//     - 增量安全检查（DetermineCheckScope）
//     - S2 历史精确定位（增量路径 + 全量路径）
//     - S2 轮次历史清洗（CleanS2TurnsForCloud）
//     - allow_cloud_reroute_after_clean 清洗后重新路由
//     - Sticky 路由复用与设置
//     - 脱敏（Step4）、执行（Step5）、审计（Step6）
//
//==============================================================================

#include "gateway.h"
#include "log.h"
#include "../../response/response_dispatcher.h"
#include "../../chat_request_handler/model_input_builder.h"
#include "../security/security_utils.h"
#include <chrono>
#include <mutex>

// ============================================================
// HandleChatCompletion：主路由入口
//
// 编排流程：
//   0.   生成 request_id，解析两级 session ID
//   0.5  用户前缀关键字检测（/cloud、/local、/企业云 等）
//   1.   检测 Agent 类型
//   2.   决定安全检查范围（全量 / 增量）
//   3.   Step1 — 敏感检测（全量或仅增量新消息）
//        [sensitivity_detection.enabled=false 时跳过，直接视为 S0]
//   4.   继承历史 max_sensitivity（增量模式）
//        [S2 历史精确定位：增量路径逐轮扫描找到第一个含 S2 的轮次]
//   5.   [全量路径] 历史 S2 预填充（当前轮干净但历史有 S2 时）
//   6.   检查 Sticky 路由（有效且当前请求非 S2 → 跳过 Step3，直接复用路由目标）
//   7.   Step2 — 复杂性评估（非 Sticky 路径）
//   8.   Step3 — 路由决策（非 Sticky 路径）
//   8.5  应用用户前缀强制路由意图（覆盖 Step3 决策）
//   9.   [S2 轮次清洗] 若路由到云端且历史有 S2 轮次 → CleanS2TurnsForCloud
//        [allow_cloud_reroute_after_clean] 清洗后重新评估路由
//  10.   UpdateSessionSecurityState（在 S2 清洗之后，使用精确的轮次起始索引）
//  11.   [Sticky 路径] 清理本地历史 → CleanLocalHistoryForCloudFallback
//  12.   Step4 — 脱敏（仅 CLOUD 路径且 need_desensitize=true）
//  13.   Step5 — 执行（LOCAL / CLOUD / ERROR）
//  14.   [首次云端路由] 若未命中 Sticky 且本次路由到云端 → 设置 Sticky session
//  15.   Step6 — 审计日志
// ============================================================
// ============================================================
// DetectClientSource：识别客户端来源
//
// 识别优先级：
//   1. HTTP 请求头 "X-Genie-Client: QAIAgentForge" → QAI_AGENT_FORGE
//   2. system prompt 含 OpenClaw 元数据块特征 → OPENCLAW
//   3. 无法识别 → UNKNOWN
// ============================================================
ClientSource GenieRoutingGateway::DetectClientSource(const httplib::Request &http_req,
                                                      const json &request)
{
    // 1. 优先检测显式请求头（QAIAgentForge 专用）
    auto it = http_req.headers.find("X-Genie-Client");
    if (it != http_req.headers.end())
    {
        const std::string &val = it->second;
        if (val == "QAIAgentForge" || val == "qaiagentforge")
        {
            My_Log{My_Log::Level::kDebug}
                << "[ClientSource] Detected QAI_AGENT_FORGE via X-Genie-Client header" << std::endl;
            return ClientSource::QAI_AGENT_FORGE;
        }
        if (val == "OpenClaw" || val == "openclaw")
        {
            My_Log{My_Log::Level::kDebug}
                << "[ClientSource] Detected OPENCLAW via X-Genie-Client header" << std::endl;
            return ClientSource::OPENCLAW;
        }
    }

    // 2. 内容特征兜底：检测 OpenClaw 特有的元数据块
    // OpenClaw 会在 user 消息中注入 "Sender (untrusted metadata):" 块
    // 或在 system prompt 中注入 OpenClaw 时间戳前缀
    if (request.contains("messages") && request["messages"].is_array())
    {
        for (const auto &msg : request["messages"])
        {
            if (!msg.is_object()) continue;
            std::string role = msg.value("role", "");
            std::string content = SecurityUtils::ExtractMessageContentText(msg);

            if (content.empty()) continue;

            // OpenClaw 特征1：user 消息含 "Sender (untrusted metadata):"
            if (role == "user" &&
                content.find("Sender (untrusted metadata):") != std::string::npos)
            {
                My_Log{My_Log::Level::kDebug}
                    << "[ClientSource] Detected OPENCLAW via 'Sender (untrusted metadata):' in user message" << std::endl;
                return ClientSource::OPENCLAW;
            }

            // OpenClaw 特征2：system prompt 含 OpenClaw 时间戳前缀格式 "[Mon 2026..."
            // 格式：[Weekday YYYY-MM-DD HH:MM GMT+N]
            if (role == "system" && content.size() > 5 && content[0] == '[')
            {
                // 简单检测：以 "[" 开头且包含 "GMT" 的 system prompt
                if (content.find("GMT") != std::string::npos &&
                    content.find("20") != std::string::npos)
                {
                    My_Log{My_Log::Level::kDebug}
                        << "[ClientSource] Detected OPENCLAW via timestamp prefix in system prompt" << std::endl;
                    return ClientSource::OPENCLAW;
                }
            }
        }
    }

    My_Log{My_Log::Level::kDebug}
        << "[ClientSource] Source UNKNOWN (no header, no OpenClaw features detected)" << std::endl;
    return ClientSource::UNKNOWN;
}

// ============================================================
// ResolvePromptPolicy：根据客户端来源和路由目标决定提示词处理策略
//
// 策略矩阵：
//   QAI_AGENT_FORGE + 任意路由目标  → OPTIMIZED
//   OPENCLAW        + LOCAL          → OPTIMIZED
//   OPENCLAW        + ENTERPRISE     → OPTIMIZED
//   OPENCLAW        + PUBLIC_CLOUD   → PASSTHROUGH（保持原有行为）
//   UNKNOWN         + PUBLIC_CLOUD   → PASSTHROUGH（保守兜底）
//   UNKNOWN         + 其他           → OPTIMIZED
// ============================================================
PromptProcessingPolicy GenieRoutingGateway::ResolvePromptPolicy(ClientSource source,
                                                                  RouteTarget target)
{
    switch (source)
    {
        case ClientSource::QAI_AGENT_FORGE:
            // QAIAgentForge：所有路由目标统一走优化流水线
            return PromptProcessingPolicy::OPTIMIZED;

        case ClientSource::OPENCLAW:
            // OpenClaw：仅公网云保持原始透传，其余走优化
            if (target == RouteTarget::PUBLIC_CLOUD)
                return PromptProcessingPolicy::PASS_THROUGH;
            return PromptProcessingPolicy::OPTIMIZED;

        case ClientSource::UNKNOWN:
        default:
            // 未知来源：公网云保守透传，其余走优化
            if (target == RouteTarget::PUBLIC_CLOUD)
                return PromptProcessingPolicy::PASS_THROUGH;
            return PromptProcessingPolicy::OPTIMIZED;
    }
}

bool GenieRoutingGateway::HandleChatCompletion(const json &request,
                                               const httplib::Request &http_req,
                                               httplib::Response &http_res,
                                               ResponseDispatcher &dispatcher,
                                               ModelInputBuilder &input_builder,
                                               bool &handled_by_cloud)
{
    auto t_start = std::chrono::steady_clock::now();
    handled_by_cloud = false;

    // ── 0. 构建请求上下文：生成 request_id 和两级 session ID ──────────────────
    InspectionContext ctx;
    ctx.request_id = GenerateRequestId();
    ResolveServerSessionIds(request, ctx);

    My_Log{} << "[GenieRoutingGateway] HandleChatCompletion start, request_id=" << ctx.request_id << std::endl;

    // ── 0.1 识别客户端来源（供后续步骤复用，避免重复解析）────────────────────
    client_source_ = DetectClientSource(http_req, request);
    const char* source_str =
        (client_source_ == ClientSource::QAI_AGENT_FORGE) ? "QAIAgentForge" :
        (client_source_ == ClientSource::OPENCLAW)         ? "OpenClaw" : "Unknown";
    My_Log{} << "[GenieRoutingGateway] Client source: " << source_str << std::endl;

    // ── 0.5 用户前缀关键字强制路由 ────────────────────────────────────────────
    // 支持的前缀关键字（不区分大小写，冒号可有可无）：
    //   - "/云:"、"/云："、"/Cloud:" → 强制使用云端模型（PUBLIC_CLOUD 优先，否则 ENTERPRISE_CLOUD）
    //   - "/企业云:"、"/enterprise:" → 强制使用企业内网云模型（ENTERPRISE_CLOUD 优先，否则 PUBLIC_CLOUD）
    //   - "/本地:"、"/本地："、"/Local:" → 强制使用本地模型
    // 检测到前缀后，从用户消息中移除前缀，并记录强制路由意图。
    enum class ForcedRouteIntent { NONE, FORCE_CLOUD, FORCE_ENTERPRISE_CLOUD, FORCE_LOCAL };
    ForcedRouteIntent forced_route = ForcedRouteIntent::NONE;
    json modified_request = request;  // 创建请求副本，用于移除前缀

    // 辅助函数：检测并移除前缀（不区分大小写，冒号可有可无）
    // 支持场景：
    //   1. 前缀直接位于字符串开头（如 "/Cloud: 查询天气"）
    //   2. 前缀前存在客户端注入的 ASCII 元数据块（如 Sender 区块）或时间戳（[Tue 2026...]）
    auto DetectAndRemovePrefix = [](const std::string& text, ForcedRouteIntent& intent) -> std::string {
        if (text.empty()) return text;

        size_t content_start = 0;

        // 1. 跳过 OpenClaw 注入的元数据块
        std::string metadata_header = "Sender (untrusted metadata):";
        if (text.find(metadata_header) == 0) {
            size_t json_start = text.find("```json\n", metadata_header.length());
            if (json_start == std::string::npos)
                json_start = text.find("```json\r\n", metadata_header.length());
            if (json_start != std::string::npos) {
                size_t closing_ticks = text.find("```", json_start + 7);
                if (closing_ticks != std::string::npos)
                    content_start = closing_ticks + 3;
            }
        }

        // 跳过前导空白
        while (content_start < text.length() &&
               (text[content_start] == ' ' || text[content_start] == '\t' ||
                text[content_start] == '\n' || text[content_start] == '\r'))
            content_start++;

        // 2. 跳过可能存在的时间戳块（例如 [Tue 2026-03-24 17:18 GMT+8]）
        if (content_start < text.length() && text[content_start] == '[') {
            size_t closing_bracket = text.find(']', content_start);
            if (closing_bracket != std::string::npos) {
                bool valid_bracket = true;
                for (size_t i = content_start + 1; i < closing_bracket; ++i) {
                    if (text[i] == '\n' || text[i] == '\r') { valid_bracket = false; break; }
                }
                if (valid_bracket)
                    content_start = closing_bracket + 1;
            }
        }

        // 再次跳过空白
        while (content_start < text.length() &&
               (text[content_start] == ' ' || text[content_start] == '\t' ||
                text[content_start] == '\n' || text[content_start] == '\r'))
            content_start++;

        if (content_start >= text.length()) return text;

        // 3. 提取实际内容，转小写用于匹配（仅对 ASCII 字符做 tolower）
        std::string remaining_text = text.substr(content_start);
        std::string remaining_lower = remaining_text;
        for (auto& c : remaining_lower) {
            unsigned char uc = static_cast<unsigned char>(c);
            if (uc < 128) c = static_cast<char>(std::tolower(uc));
        }

        // 4. 定义前缀关键字（小写），按长度降序排列（长前缀优先，避免短前缀误匹配）
        // 注意："/企业云" 必须排在 "/云" 之前，否则 "/企业云" 会被 "/云" 误匹配
        std::vector<std::pair<std::string, ForcedRouteIntent>> prefixes = {
            {"/企业云：",    ForcedRouteIntent::FORCE_ENTERPRISE_CLOUD},
            {"/企业云:",     ForcedRouteIntent::FORCE_ENTERPRISE_CLOUD},
            {"/企业云",      ForcedRouteIntent::FORCE_ENTERPRISE_CLOUD},
            {"/enterprise:", ForcedRouteIntent::FORCE_ENTERPRISE_CLOUD},
            {"/enterprise",  ForcedRouteIntent::FORCE_ENTERPRISE_CLOUD},
            {"/云：",        ForcedRouteIntent::FORCE_CLOUD},
            {"/云:",         ForcedRouteIntent::FORCE_CLOUD},
            {"/云",          ForcedRouteIntent::FORCE_CLOUD},
            {"/cloud:",      ForcedRouteIntent::FORCE_CLOUD},
            {"/cloud",       ForcedRouteIntent::FORCE_CLOUD},
            {"/本地：",      ForcedRouteIntent::FORCE_LOCAL},
            {"/本地:",       ForcedRouteIntent::FORCE_LOCAL},
            {"/本地",        ForcedRouteIntent::FORCE_LOCAL},
            {"/local:",      ForcedRouteIntent::FORCE_LOCAL},
            {"/local",       ForcedRouteIntent::FORCE_LOCAL}
        };

        // 5. 仅检查实际内容的开头是否命中前缀
        for (const auto& [prefix, route_intent] : prefixes) {
            if (remaining_lower.find(prefix) == 0) {
                intent = route_intent;
                std::string prefix_removed = remaining_text.substr(prefix.length());
                size_t first_non_space = prefix_removed.find_first_not_of(" \t\n\r");
                prefix_removed = (first_non_space != std::string::npos)
                                 ? prefix_removed.substr(first_non_space) : "";
                // 保留元数据/时间戳前缀 + 移除路由前缀后的内容
                return text.substr(0, content_start) + prefix_removed;
            }
        }
        return text;  // 未命中，返回原文
    };

    // 检查最后一条 user 消息是否包含前缀关键字
    // 兼容两种 content 结构：string 或 content parts 数组 [{type,text}]
    if (modified_request.contains("messages") && modified_request["messages"].is_array()) {
        auto& messages = modified_request["messages"];
        for (auto it = messages.rbegin(); it != messages.rend(); ++it) {
            if ((*it).value("role", "") == "user") {
                if ((*it).contains("content")) {
                    auto& content = (*it)["content"];
                    if (content.is_string()) {
                        std::string original_content = content.get<std::string>();
                        ForcedRouteIntent detected_intent = ForcedRouteIntent::NONE;
                        std::string cleaned = DetectAndRemovePrefix(original_content, detected_intent);
                        if (detected_intent != ForcedRouteIntent::NONE) {
                            forced_route = detected_intent;
                            content = cleaned;
                            const char* intent_str =
                                (forced_route == ForcedRouteIntent::FORCE_CLOUD) ? "FORCE_CLOUD" :
                                (forced_route == ForcedRouteIntent::FORCE_ENTERPRISE_CLOUD) ? "FORCE_ENTERPRISE_CLOUD" :
                                "FORCE_LOCAL";
                            My_Log{} << "[GenieRoutingGateway] User prefix detected (string content): "
                                     << intent_str
                                     << ", original_length=" << original_content.length()
                                     << ", cleaned_length=" << cleaned.length() << std::endl;
                        }
                    } else if (content.is_array()) {
                        std::string merged_text;
                        bool has_text_part = false;
                        for (const auto& part : content) {
                            if (part.is_object() && part.value("type", "") == "text" &&
                                part.contains("text") && part["text"].is_string()) {
                                if (!merged_text.empty()) merged_text += "\n";
                                merged_text += part["text"].get<std::string>();
                                has_text_part = true;
                            }
                        }
                        if (has_text_part) {
                            ForcedRouteIntent detected_intent = ForcedRouteIntent::NONE;
                            std::string cleaned = DetectAndRemovePrefix(merged_text, detected_intent);
                            if (detected_intent != ForcedRouteIntent::NONE) {
                                forced_route = detected_intent;
                                content = json::array({{{"type", "text"}, {"text", cleaned}}});
                                const char* intent_str =
                                    (forced_route == ForcedRouteIntent::FORCE_CLOUD) ? "FORCE_CLOUD" :
                                    (forced_route == ForcedRouteIntent::FORCE_ENTERPRISE_CLOUD) ? "FORCE_ENTERPRISE_CLOUD" :
                                    "FORCE_LOCAL";
                                My_Log{} << "[GenieRoutingGateway] User prefix detected (content array): "
                                         << intent_str
                                         << ", original_length=" << merged_text.length()
                                         << ", cleaned_length=" << cleaned.length() << std::endl;
                            }
                        }
                    }
                }
                break;  // 只处理最后一条 user 消息
            }
        }
    }

    // ── 1. 检测 Agent 类型 ────────────────────────────────────────────────────
    std::string agent_type = DetectAgentTypeFromRequest(modified_request);

    // ── [PERF] 分阶段计时：记录各阶段起始时间点 ──────────────────────────────
    auto t_phase = std::chrono::steady_clock::now();
    auto LogPhase = [&](const char* phase_name) {
        auto now = std::chrono::steady_clock::now();
        int64_t ms = std::chrono::duration_cast<std::chrono::milliseconds>(now - t_phase).count();
        int64_t total_ms = std::chrono::duration_cast<std::chrono::milliseconds>(now - t_start).count();
        My_Log{} << "[PERF] " << phase_name << ": phase_ms=" << ms
                 << ", total_ms=" << total_ms << std::endl;
        t_phase = now;
    };

    // ── 2. 决定安全检查范围（全量 or 增量） ──────────────────────────────────
    int last_checked_count = 0;
    CheckScope scope = DetermineCheckScope(modified_request, ctx.global_session_id, last_checked_count);

    int total_messages = 0;
    if (modified_request.contains("messages") && modified_request["messages"].is_array())
        total_messages = (int)modified_request["messages"].size();

    bool incremental_check_used = (scope == CheckScope::INCREMENTAL);
    int  incremental_new_messages = 0;
    std::string incremental_fallback_reason;

    if (scope == CheckScope::INCREMENTAL)
    {
        incremental_new_messages = total_messages - last_checked_count;
        My_Log{} << "[GenieRoutingGateway] Incremental check: "
                 << incremental_new_messages << " new messages (total=" << total_messages << ")" << std::endl;
    }
    else
    {
        My_Log{} << "[GenieRoutingGateway] Full check: total_messages=" << total_messages << std::endl;
    }

    LogPhase("Step0_PreInspect");  // [PERF] 前置处理（session解析、前缀检测、agent检测、scope决策）

    // ── 3. Step1：敏感检测 ────────────────────────────────────────────────────
    // [sensitivity_detection.enabled=false] 总开关关闭时跳过所有检测，直接视为 S0
    // 主 Agent 场景：过滤 system/developer 消息（这些消息由服务自动注入，不含用户数据）
    json request_for_inspection = FilterSystemMessagesForInspection(modified_request, agent_type);

    // 注意：ContentSecurityInspector::Inspect 内部已对每个字段做 safe_utf8_truncate(32768)，
    // 无需在此处额外截断。

    // 增量检查：只提取新增消息进行检测，复用历史安全状态
    if (scope == CheckScope::INCREMENTAL)
        request_for_inspection = ExtractIncrementalContent(request_for_inspection, last_checked_count);

    InspectionResult inspection;
    if (!routing_config_.sensitivity_detection.enabled)
    {
        // 敏感检测总开关关闭：跳过检测，所有数据视为 S0（无敏感内容），直接上云
        inspection.sensitivity_level = SensitivityLevel::S0;
        inspection.summary_reason = "sensitivity_detection disabled";
        My_Log{} << "[GenieRoutingGateway] sensitivity_detection.enabled=false, "
                 << "skipping all sensitivity checks (treating as S0)" << std::endl;
    }
    else
    {
        inspection = Step1_Inspect(request_for_inspection, ctx);
    }

    LogPhase("Step1_Inspect");  // [PERF] 敏感检测（规则引擎正则扫描）

    // ── 4. 继承历史 max_sensitivity（增量模式）+ S2 精确轮次定位 ──────────────
    // 【安全红线】：增量检查只返回"新增内容的敏感等级"，但路由决策和脱敏
    // 作用于"完整请求"。若历史中存在 S1，而增量检查返回 S0，则必须将
    // inspection.sensitivity_level 提升回 S1，否则 Step3 路由矩阵会认为
    // 无需脱敏（need_desensitize=false），导致历史 S1 数据裸发到云端！
    bool s2_history_will_be_cleaned = false;  // 标记：S2 历史是否将被清洗
    // s2_turn_start_for_update：增量路径精确定位 S2 所在轮次的起始索引
    // -2 表示"未触发精确定位"，区别于 -1（不记录轮次范围）
    int s2_turn_start_for_update = -2;

    // 找到当前轮起始 user 消息的索引（用于 S2 轮次范围记录）
    int current_turn_start = -1;
    if (modified_request.contains("messages") && modified_request["messages"].is_array())
    {
        const auto &msgs_for_turn = modified_request["messages"];
        for (int i = (int)msgs_for_turn.size() - 1; i >= 0; --i)
        {
            if (msgs_for_turn[i].value("role", "") == "user")
            {
                current_turn_start = i;
                break;
            }
        }
    }

    if (scope == CheckScope::INCREMENTAL)
    {
        std::lock_guard<std::mutex> lock(session_security_mutex_);
        auto it = session_security_states_.find(ctx.global_session_id);
        if (it != session_security_states_.end())
        {
            SensitivityLevel history_level = it->second.max_sensitivity_seen;
            incremental_fallback_reason = it->second.fallback_reason;

            // [S2 轮次清理] 若历史有 S2 且 s2_turn_ranges 非空，且当前轮无 S2
            // → 不继承 S2（S2 轮次将被清洗），标记待清洗
            if (history_level == SensitivityLevel::S2 &&
                routing_config_.s2_turn_cleaning.enabled &&
                !it->second.s2_turn_ranges.empty() &&
                inspection.sensitivity_level != SensitivityLevel::S2)
            {
                s2_history_will_be_cleaned = true;
                My_Log{} << "[GenieRoutingGateway] S2 history will be cleaned: "
                         << "s2_turns=" << it->second.s2_turn_ranges.size()
                         << ", current_turn_level=" << to_string(inspection.sensitivity_level)
                         << ", used_incremental=1" << std::endl;
                // 不继承 S2，保持当前轮的检测结果（S0 或 S1）
            }
            else if (static_cast<int>(history_level) >
                     static_cast<int>(inspection.sensitivity_level))
            {
                // 正常继承（仅增量检查路径）：历史等级高于当前轮检测结果
                inspection.sensitivity_level = history_level;
                inspection.summary_reason += " (Inherited " +
                    to_string(history_level) + " from session history)";
                My_Log{} << "[GenieRoutingGateway] Sensitivity inherited from history: "
                         << to_string(history_level)
                         << " (incremental check returned lower level)" << std::endl;
            }
        }
    }

    // ── 4.5 增量路径：S2 精确轮次定位 ────────────────────────────────────────
    // 增量检查发现 S2 时，逐轮扫描找到第一个含 S2 的轮次，精确记录起始索引
    // 用于后续 UpdateSessionSecurityState 记录正确的 S2 轮次范围
    if (scope == CheckScope::INCREMENTAL &&
        inspection.sensitivity_level == SensitivityLevel::S2 &&
        routing_config_.s2_turn_cleaning.enabled &&
        modified_request.contains("messages") && modified_request["messages"].is_array())
    {
        const auto &all_msgs = modified_request["messages"];
        int total_msgs = (int)all_msgs.size();
        int scan_end = (current_turn_start >= 0) ? current_turn_start : total_msgs;
        std::vector<int> incr_turn_starts;
        for (int si = last_checked_count; si < scan_end; ++si)
        {
            if (all_msgs[si].value("role", "") == "user")
                incr_turn_starts.push_back(si);
        }
        // 逐轮检查，找到第一个含 S2 的轮次
        // 第一个轮次从 last_checked_count 开始（覆盖上一轮结束后的 assistant 消息）
        // 后续轮次从上一个轮次的 user 消息索引开始
        int accurate_s2_turn_start = current_turn_start;  // 默认兜底：当前轮
        for (size_t ti = 0; ti < incr_turn_starts.size(); ++ti)
        {
            int scan_start = (ti == 0) ? last_checked_count : incr_turn_starts[ti - 1];
            int ti_end = (ti + 1 < incr_turn_starts.size())
                         ? incr_turn_starts[ti + 1]
                         : scan_end;
            json turn_req = json::object();
            turn_req["messages"] = json::array();
            for (int si = scan_start; si < ti_end; ++si)
                turn_req["messages"].push_back(all_msgs[si]);
            json turn_req_filtered = FilterSystemMessagesForInspection(turn_req, agent_type);
            InspectionContext ctx_s2t = ctx;
            ctx_s2t.is_internal_inspection = true;
            InspectionResult s2t_insp = Step1_Inspect(turn_req_filtered, ctx_s2t);
            if (s2t_insp.sensitivity_level == SensitivityLevel::S2)
            {
                accurate_s2_turn_start = scan_start;
                My_Log{} << "[GenieRoutingGateway] Incremental check: S2 located in turn "
                         << "scan=[" << scan_start << ", " << ti_end << ")"
                         << " (user_start=" << incr_turn_starts[ti]
                         << ", original turn_start=" << current_turn_start
                         << "), correcting s2 turn range" << std::endl;
                break;
            }
        }
        s2_turn_start_for_update = accurate_s2_turn_start;
    }

    // ── 5. 全量路径：历史 S2 预填充 ──────────────────────────────────────────
    // 全量检查发现 S2 时，区分"当前轮 S2"和"历史 S2"：
    //   - 若仅历史有 S2（当前轮干净）→ 预填充 s2_turn_ranges，标记待清洗，允许当前轮路由到云端
    //   - 若当前轮也有 S2 → 维持 LOCAL 路由
    if (!s2_history_will_be_cleaned &&
        scope == CheckScope::FULL &&
        inspection.sensitivity_level == SensitivityLevel::S2 &&
        routing_config_.s2_turn_cleaning.enabled &&
        current_turn_start >= 0)
    {
        // 提取当前轮消息，做内部安全检查
        json current_turn_content = ExtractIncrementalContent(modified_request, current_turn_start);
        json current_turn_filtered = FilterSystemMessagesForInspection(current_turn_content, agent_type);
        InspectionContext ctx_ct = ctx;
        ctx_ct.is_internal_inspection = true;
        InspectionResult current_turn_insp = Step1_Inspect(current_turn_filtered, ctx_ct);

        if (current_turn_insp.sensitivity_level != SensitivityLevel::S2)
        {
            // 当前轮干净（S0/S1），S2 仅存在于历史消息中
            // 预填充 session_security_states_，使 CleanS2TurnsForCloud 能正常工作
            {
                std::lock_guard<std::mutex> lock(session_security_mutex_);
                auto &state = session_security_states_[ctx.global_session_id];
                state.max_sensitivity_seen = SensitivityLevel::S2;
                state.incremental_mode_enabled = false;
                state.fallback_reason = "s2_detected";
                if (state.s2_turn_ranges.empty())
                {
                    const auto &hist_msgs = modified_request["messages"];
                    std::vector<int> hist_turn_starts;
                    for (int hi = 0; hi < current_turn_start; ++hi)
                    {
                        if (hist_msgs[hi].value("role", "") == "user")
                            hist_turn_starts.push_back(hi);
                    }
                    // 逐轮检查，精确记录含 S2 的轮次范围
                    // 每个轮次的扫描范围从上一个轮次的 user 消息索引开始（包含前一轮的 assistant 回复）
                    for (size_t ht = 0; ht < hist_turn_starts.size(); ++ht)
                    {
                        int scan_start = (ht == 0) ? 0 : hist_turn_starts[ht - 1];
                        int ht_end = (ht + 1 < hist_turn_starts.size())
                                     ? hist_turn_starts[ht + 1]
                                     : current_turn_start;
                        json turn_req = json::object();
                        turn_req["messages"] = json::array();
                        for (int hi = scan_start; hi < ht_end; ++hi)
                            turn_req["messages"].push_back(hist_msgs[hi]);
                        json turn_req_filtered = FilterSystemMessagesForInspection(turn_req, agent_type);
                        InspectionContext ctx_ht = ctx;
                        ctx_ht.is_internal_inspection = true;
                        InspectionResult ht_insp = Step1_Inspect(turn_req_filtered, ctx_ht);
                        if (ht_insp.sensitivity_level == SensitivityLevel::S2)
                        {
                            SessionSecurityState::S2TurnRange range;
                            range.start_idx = scan_start;
                            range.end_idx = ht_end;
                            state.s2_turn_ranges.push_back(range);
                            My_Log{} << "[GenieRoutingGateway] Full check: S2 turn located at "
                                     << "scan=[" << scan_start << ", " << ht_end << ")"
                                     << " (user_start=" << hist_turn_starts[ht] << ")" << std::endl;
                        }
                    }
                    // 保守兜底：若逐轮扫描后仍为空（极端情况），覆盖全部历史
                    if (state.s2_turn_ranges.empty())
                    {
                        SessionSecurityState::S2TurnRange range;
                        range.start_idx = 0;
                        range.end_idx = current_turn_start;
                        state.s2_turn_ranges.push_back(range);
                        My_Log{My_Log::Level::kWarning}
                            << "[GenieRoutingGateway] Full check: S2 turn scan found no S2 turns, "
                            << "falling back to full history range [0, " << current_turn_start << ")"
                            << std::endl;
                    }
                }
                state.checked_message_count = total_messages;
                state.last_updated = std::chrono::steady_clock::now();
            }

            s2_history_will_be_cleaned = true;
            inspection.sensitivity_level = current_turn_insp.sensitivity_level;

            My_Log{} << "[GenieRoutingGateway] Full check: S2 detected in history only "
                     << "(current turn=" << to_string(current_turn_insp.sensitivity_level)
                     << "), pre-populating S2 range [0, " << current_turn_start
                     << ") for cleaning, will route to CLOUD after cleaning" << std::endl;
        }
        else
        {
            My_Log{} << "[GenieRoutingGateway] Full check: S2 detected in current turn "
                     << "(current turn=" << to_string(current_turn_insp.sensitivity_level)
                     << "), maintaining LOCAL route" << std::endl;
        }
    }

    // ── 6. 检查 Sticky 路由 ───────────────────────────────────────────────────
    // 若 session 已锁定到云端且当前请求不含 S2，则跳过 Step3 路由决策，
    // 直接复用上次的路由目标，避免每轮重复执行安全检查+路由开销。
    bool sticky_route_hit = false;
    StickyRouteEntry sticky_entry_copy;
    if (!ctx.session_id.empty() && routing_config_.sticky_routing.enabled)
    {
        std::lock_guard<std::mutex> lock(sticky_sessions_mutex_);
        auto it = sticky_sessions_.find(ctx.session_id);
        if (it != sticky_sessions_.end() &&
            std::chrono::steady_clock::now() < it->second.expires_at)
        {
            if (inspection.sensitivity_level != SensitivityLevel::S2)
            {
                sticky_route_hit = true;
                sticky_entry_copy = it->second;
                My_Log{} << "[GenieRoutingGateway] Sticky route hit for session=" << ctx.session_id
                         << ", target=" << (sticky_entry_copy.target == RouteTarget::ENTERPRISE_CLOUD
                                            ? "ENTERPRISE_CLOUD" : "PUBLIC_CLOUD")
                         << ", fallback_msg_count=" << sticky_entry_copy.fallback_msg_count << std::endl;
            }
            else
            {
                // S2 检测到 → 清除 sticky session，后续按正常路由决策处理
                sticky_sessions_.erase(it);
                My_Log{My_Log::Level::kWarning}
                    << "[GenieRoutingGateway] Sticky route cleared due to S2 detection for session="
                    << ctx.session_id << std::endl;
            }
        }
    }

    // ── 7 & 8. Step2 + Step3：复杂性评估 + 路由决策（非 Sticky 路径）──────────
    ComplexityResult complexity;
    RouteDecision decision;

    if (sticky_route_hit)
    {
        // Sticky 路径：跳过 Step2/Step3，复用已锁定的路由目标
        complexity.complexity_level = ComplexityLevel::C0;
        complexity.reason = "sticky_route_hit";
        decision.target          = sticky_entry_copy.target;
        decision.decision_reason = "sticky_route_session_locked";
        // S1 内容上云前须脱敏（仅企业云且配置要求脱敏时执行）
        bool going_enterprise = (sticky_entry_copy.target == RouteTarget::ENTERPRISE_CLOUD);
        decision.need_desensitize = (inspection.sensitivity_level == SensitivityLevel::S1) &&
                                    (!going_enterprise || routing_config_.enterprise_cloud_require_desensitize);
        decision.local_available  = true;
    }
    else
    {
        // 对完整请求（含 tools 等）执行复杂性评估，不过滤 system 消息
        complexity = Step2_EvaluateComplexity(modified_request, ctx);
        decision = Step3_Route(inspection, complexity, agent_type);
    }

    LogPhase("Step2_3_RouteDecision");  // [PERF] 复杂性评估 + 路由决策

    // ── 8.5 应用用户前缀强制路由意图（在 Step3 路由决策之后覆盖）────────────────
    // 安全策略优先：S2 内容始终强制本地，不受用户前缀影响。
    // Sticky session 命中时跳过强制路由（避免干扰已锁定的会话路由）。
    if (forced_route != ForcedRouteIntent::NONE && !sticky_route_hit)
    {
        RouteTarget original_target = decision.target;

        if (forced_route == ForcedRouteIntent::FORCE_CLOUD)
        {
            if (inspection.sensitivity_level == SensitivityLevel::S2)
            {
                // S2 内容不允许上云，安全策略覆盖用户意图
                My_Log{My_Log::Level::kWarning}
                    << "[GenieRoutingGateway] User requested FORCE_CLOUD, but S2 content detected. "
                    << "Overriding to LOCAL (security policy)" << std::endl;
                decision.target = RouteTarget::LOCAL;
                decision.decision_reason = "user_force_cloud_overridden_by_s2_policy";
            }
            else
            {
                // 优先选公有云，不可用时选企业云，再不可用则 fallback LOCAL
                bool public_avail     = cloud_client_.IsAvailable();
                bool enterprise_avail = enterprise_cloud_client_.IsAvailable();

                if (public_avail)
                {
                    decision.target = RouteTarget::PUBLIC_CLOUD;
                    decision.need_desensitize = (inspection.sensitivity_level == SensitivityLevel::S1);
                    decision.decision_reason = "user_force_cloud";
                    My_Log{} << "[GenieRoutingGateway] User prefix: forcing PUBLIC_CLOUD route (original="
                             << to_string(original_target) << ")" << std::endl;
                }
                else if (enterprise_avail)
                {
                    decision.target = RouteTarget::ENTERPRISE_CLOUD;
                    decision.need_desensitize = (inspection.sensitivity_level == SensitivityLevel::S1)
                                                && routing_config_.enterprise_cloud_require_desensitize;
                    decision.decision_reason = "user_force_cloud_via_enterprise";
                    My_Log{} << "[GenieRoutingGateway] User prefix: forcing ENTERPRISE_CLOUD route "
                             << "(public cloud unavailable, original=" << to_string(original_target) << ")" << std::endl;
                }
                else
                {
                    // 所有云端均不可用，fallback LOCAL
                    My_Log{My_Log::Level::kWarning}
                        << "[GenieRoutingGateway] User requested FORCE_CLOUD, but all clouds are unavailable. "
                        << "Falling back to LOCAL" << std::endl;
                    decision.target = RouteTarget::LOCAL;
                    decision.decision_reason = "user_force_cloud_but_all_clouds_unavailable";
                }
            }
        }
        else if (forced_route == ForcedRouteIntent::FORCE_ENTERPRISE_CLOUD)
        {
            if (inspection.sensitivity_level == SensitivityLevel::S2)
            {
                // S2 内容不允许上云，安全策略覆盖用户意图
                My_Log{My_Log::Level::kWarning}
                    << "[GenieRoutingGateway] User requested FORCE_ENTERPRISE_CLOUD, but S2 content detected. "
                    << "Overriding to LOCAL (security policy)" << std::endl;
                decision.target = RouteTarget::LOCAL;
                decision.decision_reason = "user_force_enterprise_cloud_overridden_by_s2_policy";
            }
            else
            {
                // 优先选企业云，不可用时选公有云，再不可用则 fallback LOCAL
                bool enterprise_avail = enterprise_cloud_client_.IsAvailable();
                bool public_avail     = cloud_client_.IsAvailable();

                if (enterprise_avail)
                {
                    decision.target = RouteTarget::ENTERPRISE_CLOUD;
                    decision.need_desensitize = (inspection.sensitivity_level == SensitivityLevel::S1)
                                                && routing_config_.enterprise_cloud_require_desensitize;
                    decision.decision_reason = "user_force_enterprise_cloud";
                    My_Log{} << "[GenieRoutingGateway] User prefix: forcing ENTERPRISE_CLOUD route (original="
                             << to_string(original_target) << ")" << std::endl;
                }
                else if (public_avail)
                {
                    decision.target = RouteTarget::PUBLIC_CLOUD;
                    decision.need_desensitize = (inspection.sensitivity_level == SensitivityLevel::S1);
                    decision.decision_reason = "user_force_enterprise_cloud_fallback_public";
                    My_Log{} << "[GenieRoutingGateway] User prefix: ENTERPRISE_CLOUD unavailable, "
                             << "falling back to PUBLIC_CLOUD (original=" << to_string(original_target) << ")" << std::endl;
                }
                else
                {
                    // 所有云端均不可用，fallback LOCAL
                    My_Log{My_Log::Level::kWarning}
                        << "[GenieRoutingGateway] User requested FORCE_ENTERPRISE_CLOUD, but all clouds are unavailable. "
                        << "Falling back to LOCAL" << std::endl;
                    decision.target = RouteTarget::LOCAL;
                    decision.decision_reason = "user_force_enterprise_cloud_but_all_clouds_unavailable";
                }
            }
        }
        else if (forced_route == ForcedRouteIntent::FORCE_LOCAL)
        {
            if (!local_model_config_.enabled || !local_availability_checker_())
            {
                My_Log{My_Log::Level::kWarning}
                    << "[GenieRoutingGateway] User requested FORCE_LOCAL, but local model is unavailable. "
                    << "Returning error" << std::endl;
                decision.target = RouteTarget::POLICY_ERROR;
                decision.error_code = "user_force_local_but_local_unavailable";
                decision.error_http_status = 503;
                decision.decision_reason = "user_force_local_but_local_unavailable";
            }
            else
            {
                decision.target = RouteTarget::LOCAL;
                decision.need_desensitize = false;
                decision.decision_reason = "user_force_local";
                My_Log{} << "[GenieRoutingGateway] User prefix: forcing LOCAL route (original="
                         << to_string(original_target) << ")" << std::endl;
            }
        }
    }

    // ── 9. 云端路由前处理：S2 轮次清洗 ──────────────────────────────────────
    json request_for_cloud = modified_request;
    bool is_cloud_target = (decision.target == RouteTarget::ENTERPRISE_CLOUD ||
                            decision.target == RouteTarget::PUBLIC_CLOUD);
    bool s2_cleaning_occurred = false;

    if (is_cloud_target && s2_history_will_be_cleaned)
    {
        // [S2 轮次清洗] 历史有 S2 轮次，清洗后再上云
        json s2_cleaned = CleanS2TurnsForCloud(
            request_for_cloud, ctx.global_session_id,
            current_turn_start, inspection.sensitivity_level,
            "HandleChatCompletion");

        s2_cleaning_occurred = s2_cleaned.contains("messages") &&
                               s2_cleaned["messages"] != request_for_cloud["messages"];

        if (s2_cleaning_occurred)
        {
            // 清洗后对清洗后的历史做一次全量安全检查，确定实际最高等级
            json cleaned_for_check = FilterSystemMessagesForInspection(s2_cleaned, agent_type);
            InspectionContext ctx_recheck = ctx;
            ctx_recheck.is_internal_inspection = true;
            InspectionResult cleaned_inspection = Step1_Inspect(cleaned_for_check, ctx_recheck);

            if (cleaned_inspection.sensitivity_level == SensitivityLevel::S2)
            {
                // 防御性检查：清洗后仍有 S2（理论上不应发生），强制回退到 LOCAL
                My_Log{My_Log::Level::kError}
                    << "[GenieRoutingGateway] S2 turn cleaning: re-inspection found S2 in cleaned history! "
                    << "Forcing LOCAL route to prevent S2 data leakage." << std::endl;
                decision.target = RouteTarget::LOCAL;
                decision.need_desensitize = false;
                decision.sensitivity = SensitivityLevel::S2;
                decision.decision_reason += " (Fallback: S2 found in cleaned history, forced LOCAL)";
                is_cloud_target = false;
                s2_cleaning_occurred = false;
                // request_for_cloud 保持原始请求（走本地）
            }
            else
            {
                // 清洗成功：使用清洗后的请求
                request_for_cloud = s2_cleaned;

                // 若清洗后等级升高，更新 inspection 和 session 状态
                if (static_cast<int>(cleaned_inspection.sensitivity_level) >
                    static_cast<int>(inspection.sensitivity_level))
                {
                    inspection.sensitivity_level = cleaned_inspection.sensitivity_level;
                    inspection.summary_reason += " (Re-inspected after S2 turn cleaning: " +
                        to_string(cleaned_inspection.sensitivity_level) + ")";
                    {
                        std::lock_guard<std::mutex> lock(session_security_mutex_);
                        auto it = session_security_states_.find(ctx.global_session_id);
                        if (it != session_security_states_.end())
                            it->second.max_sensitivity_seen = inspection.sensitivity_level;
                    }
                }
                decision.sensitivity = inspection.sensitivity_level;

                // [allow_cloud_reroute_after_clean] 清洗后重新评估路由
                // 仅在非 Sticky、非强制路由场景下执行（强制路由意图已明确，无需重新评估）
                if (decision.target == RouteTarget::LOCAL &&
                    routing_config_.s2_turn_cleaning.allow_cloud_reroute_after_clean &&
                    forced_route == ForcedRouteIntent::NONE)
                {
                    My_Log{} << "[GenieRoutingGateway] S2 turn cleaning: re-evaluating route after history cleaned "
                             << "(original decision=LOCAL, checking if CLOUD is now appropriate)..." << std::endl;
                    RouteDecision new_decision = Step3_Route(inspection, complexity, agent_type);

                    if (new_decision.target == RouteTarget::ENTERPRISE_CLOUD ||
                        new_decision.target == RouteTarget::PUBLIC_CLOUD)
                    {
                        My_Log{} << "[GenieRoutingGateway] S2 turn cleaning: re-routing LOCAL→"
                                 << to_string(new_decision.target) << " confirmed, "
                                 << "new_reason=" << new_decision.decision_reason << std::endl;
                        decision = new_decision;
                        is_cloud_target = true;
                    }
                    else
                    {
                        My_Log{} << "[GenieRoutingGateway] S2 turn cleaning: re-routing evaluated, staying LOCAL "
                                 << "(reason=" << new_decision.decision_reason << ")" << std::endl;
                    }
                }

                // 根据最终路由目标更新 need_desensitize
                if (inspection.sensitivity_level == SensitivityLevel::S1)
                {
                    bool going_enterprise = (decision.target == RouteTarget::ENTERPRISE_CLOUD);
                    decision.need_desensitize = is_cloud_target &&
                        (!going_enterprise || routing_config_.enterprise_cloud_require_desensitize);
                }
                else if (inspection.sensitivity_level == SensitivityLevel::S0)
                {
                    decision.need_desensitize = false;
                }
            }
        }
        else
        {
            // CleanS2TurnsForCloud 未实际修改消息（可能 s2_turn_ranges 已被清空）
            // request_for_cloud 保持 modified_request
        }
    }
    else if (is_cloud_target)
    {
        // 无 S2 历史清洗需求，但仍需检查是否有残留 S2 轮次（来自之前轮次的记录）
        bool has_s2_turns = false;
        if (routing_config_.s2_turn_cleaning.enabled && !ctx.global_session_id.empty())
        {
            std::lock_guard<std::mutex> lock(session_security_mutex_);
            auto it = session_security_states_.find(ctx.global_session_id);
            has_s2_turns = (it != session_security_states_.end() &&
                            !it->second.s2_turn_ranges.empty());
        }
        if (has_s2_turns)
        {
            request_for_cloud = CleanS2TurnsForCloud(
                request_for_cloud, ctx.global_session_id,
                current_turn_start, inspection.sensitivity_level,
                "HandleChatCompletion");
        }
    }

    // ── 10. UpdateSessionSecurityState（在 S2 清洗之后，使用精确的轮次起始索引）──
    {
        int turn_start_for_update = current_turn_start;
        // 若增量路径精确定位了 S2 所在轮次，用精确值覆盖
        if (s2_turn_start_for_update != -2)
            turn_start_for_update = s2_turn_start_for_update;

        UpdateSessionSecurityState(ctx.global_session_id, inspection,
                                   total_messages, turn_start_for_update);
    }

    // ── 11. Sticky 历史清理 ───────────────────────────────────────────────────
    // sticky session 命中时清理本地工具调用历史
    if (is_cloud_target && sticky_route_hit)
    {
        request_for_cloud = CleanLocalHistoryForCloudFallback(
            request_for_cloud, sticky_entry_copy.fallback_msg_count);
    }

    // ── 12. Step4：脱敏（仅 CLOUD 路径且 need_desensitize=true）──────────────
    DesensitizationResult desensitized;
    desensitized.success = true;  // LOCAL 路径默认不需要脱敏

    if (is_cloud_target && decision.need_desensitize)
    {
        LogPhase("Step4_PreDesensitize");  // [PERF] S2清洗/sticky清理等云端前处理
        // 修复：使用 DesensitizeForCloudOnly 替代 Step4_Desensitize。
        // 原因：Step4_Desensitize 会对所有消息（包括 system 消息）执行脱敏，
        // 而 system 消息中的 <available_skills> XML 包含 Windows 路径（如 C:\Users\...\SKILL.md），
        // R_PATH_WIN 正则 \S{3,} 会贪婪地匹配到 XML 结束标签 </location>，
        // 导致脱敏替换时将 </location> 一并消耗，破坏 XML 结构，
        // 最终 ParseAvailableSkillsXml 解析到 0 skills，Skill Catalog 丢失。
        //
        // DesensitizeForCloudOnly 在 main agent 场景下会：
        //   1. 过滤掉 system/developer 消息（服务端注入，不含用户数据）
        //   2. 对剩余消息（user/assistant/tool）执行脱敏
        //   3. 将原始 system/developer 消息插回脱敏结果头部
        // 这样 <available_skills> XML 完全不参与脱敏，不受正则贪婪问题影响。
        desensitized = DesensitizeForCloudOnly(request_for_cloud, inspection, ctx,
                                               agent_type, "HandleChatCompletion");
        LogPhase("Step4_Desensitize");  // [PERF] 脱敏（迭代正则替换）
        if (!desensitized.success)
        {
            My_Log{My_Log::Level::kError}
                << "[GenieRoutingGateway] Desensitization failed (" << desensitized.failure_reason
                << "), falling back to LOCAL or ERROR" << std::endl;

            // 脱敏失败策略：本地可用 → 回退 LOCAL；否则 → POLICY_ERROR
            bool local_avail = local_model_config_.enabled && local_availability_checker_();
            if (local_avail)
            {
                decision.target           = RouteTarget::LOCAL;
                decision.need_desensitize = false;
                decision.decision_reason  = "desensitization_failed_fallback_local";
                is_cloud_target           = false;
            }
            else
            {
                decision.target           = RouteTarget::POLICY_ERROR;
                decision.error_code       = "desensitization_failed";
                decision.error_http_status = 403;
                decision.local_available  = false;
            }
        }
    }

    // ── 13. Step5：执行（LOCAL / CLOUD / ERROR）───────────────────────────────
    LogPhase("Step5_PreExecute");  // [PERF] 路由前处理总耗时（含安全扫描、脱敏等）
    bool ok = Step5_Execute(decision,
                            request_for_cloud,
                            desensitized,
                            http_req, http_res,
                            dispatcher, input_builder,
                            handled_by_cloud,
                            ctx.session_id);

    // ── 14. 首次云端路由成功 → 设置 Sticky session ───────────────────────────
    // HandleLocalOutputOverflow / HandleLocalInputOverflow 也会设置 sticky，
    // 此处仅处理 HandleChatCompletion 正常路由到云端的场景（sticky 未命中时）。
    if (handled_by_cloud && !sticky_route_hit &&
        routing_config_.sticky_routing.enabled && !ctx.session_id.empty())
    {
        std::lock_guard<std::mutex> lock(sticky_sessions_mutex_);
        if ((int)sticky_sessions_.size() >= routing_config_.sticky_routing.max_sessions)
            sticky_sessions_.erase(sticky_sessions_.begin());

        StickyRouteEntry entry;
        entry.target      = decision.target;
        entry.sensitivity = inspection.sensitivity_level;
        entry.expires_at  = std::chrono::steady_clock::now() +
                            std::chrono::seconds(routing_config_.sticky_routing.ttl_seconds);
        entry.fallback_msg_count = total_messages;
        sticky_sessions_[ctx.session_id] = entry;

        My_Log{} << "[GenieRoutingGateway] Sticky route set for session=" << ctx.session_id
                 << ", target=" << (entry.target == RouteTarget::ENTERPRISE_CLOUD
                                    ? "ENTERPRISE_CLOUD" : "PUBLIC_CLOUD")
                 << ", fallback_msg_count=" << entry.fallback_msg_count << std::endl;
    }

    // ── 15. Step6：审计日志 ───────────────────────────────────────────────────
    auto t_end = std::chrono::steady_clock::now();
    int64_t latency_ms = std::chrono::duration_cast<std::chrono::milliseconds>(t_end - t_start).count();

    // http_status：LOCAL 路径无云端 HTTP 状态（-1），CLOUD 路径由 ExecuteCloudRequest 内部管理
    int audit_http_status = handled_by_cloud ? 200 : -1;

    Step6_Audit(ctx, inspection, complexity, decision, desensitized,
                latency_ms,
                audit_http_status,
                sticky_route_hit,
                0, 0, 0,    // 云端 token 计数由 ExecuteCloudRequest 内部管理
                incremental_check_used,
                incremental_new_messages,
                total_messages,
                incremental_fallback_reason);

    return ok;
}
