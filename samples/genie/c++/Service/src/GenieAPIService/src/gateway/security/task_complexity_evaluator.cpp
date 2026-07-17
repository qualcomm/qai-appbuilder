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

// 从 messages 中提取最新一条 user 消息的文本内容
// 用于复杂度评估：只根据用户最新输入判断任务复杂度，避免历史消息干扰
std::string TaskComplexityEvaluator::ExtractLastUserMessage(const json &messages)
{
    if (!messages.is_array())
        return "";

    // 从后往前找最后一条 role=user 的消息
    for (int i = static_cast<int>(messages.size()) - 1; i >= 0; --i)
    {
        const auto &msg = messages[i];
        if (!msg.contains("role") || !msg["role"].is_string())
            continue;
        if (msg["role"].get<std::string>() != "user")
            continue;

        // 找到最后一条 user 消息，提取其文本内容
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
    // 优先使用 handle->TokenLength() 精确计算 token 数量
    // 仅当模型句柄不可用时，才退化为字符数估算（1 token ≈ 3 字符）
    std::string all_text = ExtractAllText(messages);
    if (tools.is_array())
    {
        all_text += tools.dump();
    }

    if (model_config_)
    {
        // 修复：使用 GetDefaultModelHandle() 确保始终使用 default 模型进行 token 计算
        auto handle = model_config_->GetDefaultModelHandle().lock();
        if (handle)
        {
            // 使用模型 tokenizer 精确计算，结果准确
            return static_cast<int>(handle->TokenLength(all_text));
        }
    }

    // 降级：字符数 / 3 粗估（仅在模型句柄不可用时使用）
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
    // 关键词完全从配置文件读取（service_config.json routing.complexity.keywords_c1/c2）
    // 若配置为空，则跳过关键词匹配，直接返回 C0
    const std::vector<std::string> &complex_keywords_c2 = config_.keywords_c2;
    const std::vector<std::string> &complex_keywords_c1 = config_.keywords_c1;

    std::string all_text = ExtractAllText(messages);

    // 转换为小写进行匹配
    std::string lower_text = all_text;
    std::transform(lower_text.begin(), lower_text.end(), lower_text.begin(),
                   [](unsigned char c) { return std::tolower(c); });

    // 先检查 C2 关键词（优先级更高）
    for (const auto &kw : complex_keywords_c2)
    {
        // kw 已经是小写，直接匹配
        if (lower_text.find(kw) != std::string::npos)
        {
            return ComplexityLevel::C2;
        }
    }

    // 再检查 C1 关键词
    for (const auto &kw : complex_keywords_c1)
    {
        // kw 已经是小写，直接匹配
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

    // 注意：不再基于原始 messages 的 token 数量判断复杂度。
    // 原因：原始 messages 包含完整聊天历史、系统提示词、工具定义等，token 数量虚高，
    // 会导致简单问答（如"你好"）被误判为 C2 并路由到云端。
    // 正确做法：若压缩后的 prompt 仍超出本地上下文窗口，ModelInputBuilder::Build()
    // 会抛出"Cannot compress further"异常，由 HandleLocalInputOverflow 触发预路由回退到云端。

    // 1. 检查 最新用户提问之后的 tool_calls 数量
    // 使用 complexity.thresholds.tool_calls（复杂度评估软阈值），
    // 而非 agent_routing.max_tool_call_retries（防无限循环硬限制，语义不同）
    int tool_call_count = CountToolCallsAfterLastUser(messages);
    int tool_calls_threshold = config_.thresholds.tool_calls;  // 默认值 5，来自 service_config.json

    // 2. 检查复杂关键词（C2 关键词直接升级为 C2，C1 关键词升级为 C1）
    // 只针对最新一条 user 消息进行关键词匹配，避免历史消息中的关键词干扰当前任务的复杂度判断。
    // 例如：历史消息中包含 "summarize" 不应导致当前简单请求被误判为 C1。
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

// ============================================================
// 本地模型辅助判断
// ============================================================

// 提取任务摘要，按 max_input_tokens 截断原文
// 优先只取最新一条 user 消息；若 max_input_tokens 有余量，可向前追加更多消息。
// 若有多条消息，丢弃旧消息，只保留新消息，直到内容长度 <= max_input_tokens。
std::string TaskComplexityEvaluator::ExtractTaskSummary(
        const json &messages,
        const json &tools) const
{
    // 使用 SecurityUtils::CleanMessageText 统一处理（与 content_security_inspector.cpp 共用）：
    //   步骤1：过滤 "Sender (untrusted metadata):" JSON 块（含 trim）
    //   步骤2：过滤 OpenClaw 时间戳前缀 "[Wed 2026-03-25 20:13 GMT+8] "
    //   步骤3：再次 trim
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

    // 获取模型句柄用于 token 计数
    // 若模型句柄可用，使用 token 限制；否则退化为字符数限制
    std::shared_ptr<ContextBase> handle;
    size_t max_input_tokens = 0;
    if (model_config_) {
        auto h = model_config_->GetDefaultModelHandle().lock();
        if (h) {
            handle = h;
            int ctx_size = model_config_->context_size();
            size_t context_size = (ctx_size > 0) ? static_cast<size_t>(ctx_size) : 8192;
            // 与 ContentSecurityInspector 保持一致：max_input_tokens = context_size * 4 / 5
            max_input_tokens = context_size * 4 / 5;
        }
    }

    if (handle && max_input_tokens > 0) {
        // ── Token-based 实现：从最新消息往前拼接，丢弃旧消息 ──────────────
        // 只处理非 system 消息（system prompt 不含用户意图信息）
        // 优先保留最新的 user 消息，向前追加直到达到 token 预算
        My_Log{} << "[TaskComplexityEvaluator] LocalModel input limit: "
                 << "context_size=" << (model_config_ ? model_config_->context_size() : 0)
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
                // 截断到剩余 token 预算
                // 按比例估算截断字符数
                size_t estimated_chars = text.size() * remaining_tokens / text_tokens;
                std::string truncated = safe_utf8_truncate(text, estimated_chars, "");
                // 迭代调整（最多 3 次）
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

        // 反转片段顺序，恢复时间顺序（最旧的在前，最新的在后）
        std::reverse(segments.begin(), segments.end());

        std::string full_text;
        for (const auto& s : segments) {
            full_text += s;
            full_text += ' ';
        }
        // 去除末尾多余空格
        if (!full_text.empty() && full_text.back() == ' ') {
            full_text.pop_back();
        }
        return full_text;

    } else {
        // ── 字符数回退实现（模型句柄不可用时）──────────────────────────
        // 只取最新一条 user 消息，按字符数截断
        size_t max_chars = static_cast<size_t>(config_.model_input_max_chars);
        std::string full_text;

        for (int i = static_cast<int>(messages.size()) - 1; i >= 0; --i) {
            const auto& msg = messages[i];
            if (!msg.contains("role") || !msg["role"].is_string()) continue;
            if (msg["role"].get<std::string>() != "user") continue;

            full_text = extract_msg_text(msg);
            break;  // 只取最后一条 user 消息
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

    // 将命中项移到链表头部（LRU 更新）
    cache_list_.splice(cache_list_.begin(), cache_list_, it->second);
    entry = it->second->second;
    return true;
}

void TaskComplexityEvaluator::CacheInsert(const std::string &key, const CacheEntry &entry)
{
    std::lock_guard<std::mutex> lock(cache_mutex_);

    // 若已存在，先删除旧项
    auto it = cache_map_.find(key);
    if (it != cache_map_.end())
    {
        cache_list_.erase(it->second);
        cache_map_.erase(it);
    }

    // 插入到链表头部
    cache_list_.push_front({key, entry});
    cache_map_[key] = cache_list_.begin();

    // 超出容量时，删除链表尾部（最久未使用）
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

    // 获取模型句柄（weak_ptr → shared_ptr）
    // 修复：使用 GetDefaultModelHandle() 确保复杂度评估始终使用 default 模型
    auto handle = model_config_->GetDefaultModelHandle().lock();
    if (!handle)
    {
        reason = "default model handle not available";
        return false;
    }

    // 构建 Prompt（不含原始敏感内容，仅传入任务摘要）
    // 根据模型类型选择不同的提示词格式，与主推理过程保持一致
    // 系统提示词从 service_config.json 的 routing.complexity.system_prompt 读取

    // 对所有模型（Harmony 和 General）统一传入 json_prefill，
    // 引导模型直接输出 JSON 内容，避免模型输出无关文本。
    // - Harmony 模型：注入到 assistant prefill（final 通道标记之后）
    // - General 模型：追加到 start_prompt 末尾（<|im_start|>assistant\n 之后）
    // 与 content_security_inspector.cpp 的处理方式完全一致。
    const bool is_harmony_model = (model_config_->get_prompt_type() == PromptType::Harmony);
    const std::string complexity_json_prefill = "{\"complexity\":\"";
    // 将 "Task summary: " 改为 "Text: "，与安全检查（content_security_inspector.cpp）的
    // prompt 前缀完全对齐。
    // 原因：QNN 小模型（Qwen2.0-7B-SSD，中文模型）看到 "Task summary: hello" 时，
    // 将 "Task summary:" 理解为中文语境下的"任务摘要："提示语，认为输入不完整，
    // 输出 "输入"（提示用户继续输入）而非 JSON。
    // 安全检查使用 "Text: " 前缀，模型能正确理解为"对以下文本进行分类"，
    // 复杂度检查统一使用相同前缀，确保两个检查的 prompt 格式完全一致。
    std::string prompt = BuildLocalModelPrompt(model_config_, config_.system_prompt, "Text: " + task_summary,
                                               complexity_json_prefill);
    My_Log{} << "[TaskComplexityEvaluator] prompt format: "
             << (is_harmony_model ? "Harmony" : "General")
             << " for complexity check" << std::endl;

    // 使用共享的本地模型推理框架（SecurityUtils::LocalModelQuery）：
    // 内部已通过 std::thread + std::promise 实现真正的非阻塞超时控制，
    // 此处只需提供早停判定的 token 回调。原实现未设置硬上限，max_gen_tokens 传 0（不限制，由 timeout_ms 控制）。
    auto query_result = SecurityUtils::LocalModelQuery(
            handle, prompt, complexity_json_prefill, timeout_ms, /*max_gen_tokens=*/0,
            [](std::string &output_buf, const std::string &token, bool &json_complete) -> bool {
                (void)token;
                // 早停：检测到完整且合法的 JSON 对象后立即停止生成
                // 复杂度检查期望的输出格式：{"complexity":"C0|C1|C2","reason":"..."}
                //
                // 策略1：完整 JSON 解析（标准路径）
                // 策略2：前缀匹配早停（防御路径，复用 SecurityUtils::CheckPrefixEarlyStop）
                //   当 output_buf 以 {"complexity":" 开头，且紧跟 C0/C1/C2 后出现非字母字符时，
                //   说明模型已输出了有效的 complexity 值，可以提前停止。
                //   这可以防止模型在输出 C0/C1/C2 后继续生成奇怪内容（如 "([{"）。
                // 策略1：完整 JSON 解析
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

                // 策略2：前缀匹配早停
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

    // 解析模型输出 JSON
    // output_buf 已初始化为 {"complexity":"（prefill 前缀），
    // 因此 model_output 中的内容是 {"complexity":"C0","reason":"..."} 格式，
    // 直接从 model_output 开头搜索 { 即可。
    const std::string &model_output = query_result.output;
    try
    {
        // 查找 JSON 对象的起始和结束位置
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
    // is_internal_inspection 仅用于防模型调用递归，不应跳过启发式评估

    // 启发式评估
    ComplexityResult result = HeuristicEvaluate(messages, tools);

    // ---- 本地模型辅助判断 ----
    // 仅在 is_internal_inspection 为 false 时调用模型
    if (!ctx.is_internal_inspection &&
        config_.use_local_model_fallback &&
        result.complexity_level == ComplexityLevel::C0 &&
        model_config_ != nullptr)
    {
        // 提取任务摘要（不含原始敏感内容）
        std::string task_summary = ExtractTaskSummary(messages, tools);

        if (!task_summary.empty())
        {
            // 查询 LRU 缓存（以内容哈希为 key，不缓存原文）
            std::string cache_key = ComputeHash(task_summary);
            CacheEntry cached_entry;

            if (CacheLookup(cache_key, cached_entry))
            {
                // 缓存命中
                My_Log{} << "[TaskComplexityEvaluator] LocalModel cache hit, level="
                         << to_string(cached_entry.level) << std::endl;
                // 取启发式结果与缓存结果的较高者
                if (static_cast<int>(cached_entry.level) > static_cast<int>(result.complexity_level))
                {
                    result.complexity_level = cached_entry.level;
                    result.reason = "local_model_cached: " + cached_entry.reason;
                }
            }
            else
            {
                // 缓存未命中，调用本地模型
                ComplexityLevel model_level = ComplexityLevel::C0;
                std::string model_reason;

                bool model_ok = LocalModelEvaluate(
                    task_summary,
                    config_.timeout_ms,  // 从配置读取超时（service_config.json routing.complexity.timeout_ms）
                    model_level,
                    model_reason);

                if (model_ok)
                {
                    // 缓存结果（不缓存原文，仅缓存哈希→结果映射）
                    CacheInsert(cache_key, {model_level, model_reason});

                    My_Log{} << "[TaskComplexityEvaluator] LocalModel result: level="
                             << to_string(model_level) << ", reason=" << model_reason << std::endl;

                    // 取启发式结果与模型结果的较高者（保守策略）
                    if (static_cast<int>(model_level) > static_cast<int>(result.complexity_level))
                    {
                        result.complexity_level = model_level;
                        result.reason = "local_model: " + model_reason;
                    }
                }
                else
                {
                    // =========================================================
                    // 缺口3：本地模型辅助判定失败 → 保守降级策略
                    // 将启发式结果提升一级（C0→C1，C1→C2）
                    // 若启发式也未命中（result.complexity_level==C0），强制升级为 C1
                    // 依据：§七-A 降级策略 - 复杂性评估辅助失败时的保守处理
                    // =========================================================
                    ComplexityLevel elevated_level;
                    if (result.complexity_level == ComplexityLevel::C0)
                        elevated_level = ComplexityLevel::C1;  // C0 → C1（启发式无命中时强制 C1）
                    else if (result.complexity_level == ComplexityLevel::C1)
                        elevated_level = ComplexityLevel::C2;  // C1 → C2
                    else
                        elevated_level = ComplexityLevel::C2;  // C2 保持不变

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
        // 本地模型辅助判断被跳过，记录原因
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
