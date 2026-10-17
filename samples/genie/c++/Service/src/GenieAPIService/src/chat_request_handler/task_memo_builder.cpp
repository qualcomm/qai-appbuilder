//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "task_memo_builder.h"
#include "tool_call_circuit_breaker_store.h"
#include <utils.h>
#include "log.h"
#include <sstream>
#include <iomanip>
#include <algorithm>
#include <functional>
#include <cstring>

TaskMemoBuilder::TaskMemoBuilder(
    const PromptOptimizationConfig::TaskMemoConfig& config,
    const ModelInstanceConfig& instance_config,
    TaskMemoStore* store,
    InferFn infer_fn,
    IsAliveFn is_alive_fn)
    : config_(config)
    , instance_config_(instance_config)
    , store_(store)
    , infer_fn_(std::move(infer_fn))
    , is_alive_fn_(std::move(is_alive_fn))
{
}

// ── 会话指纹（技法对齐 GenieRoutingGateway::ComputeHistoryFingerprint/ComputeFirstMsgKey，
//    本地独立实现，不依赖 GenieRoutingGateway 模块，前缀 tmfp_/tmfmk_ 与 gateway 自身的
//    fmk_ 区分，避免日志排查时误认为同一个哈希空间）──────────────────────────────

std::string TaskMemoBuilder::ComputeFingerprint(const json& messages)
{
    if (!messages.is_array() || messages.empty())
        return "";

    std::vector<const json*> non_system;
    for (const auto& msg : messages) {
        const std::string role = msg.value("role", "");
        if (role != "system" && role != "developer")
            non_system.push_back(&msg);
    }
    if (non_system.size() <= 1)
        return "";

    uint64_t hash = 14695981039346656037ULL;
    const uint64_t prime = 1099511628211ULL;
    auto hash_string = [&](const std::string& s) {
        for (unsigned char c : s) { hash ^= c; hash *= prime; }
        hash ^= 0xFF; hash *= prime;
    };

    for (size_t i = 0; i < non_system.size() - 1; ++i) {
        const auto& msg = *non_system[i];
        hash_string(msg.value("role", ""));
        if (msg.contains("content")) {
            hash_string(msg["content"].is_string() ? msg["content"].get<std::string>() : msg["content"].dump());
        }
    }

    std::ostringstream oss;
    oss << "tmfp_" << std::hex << std::setfill('0') << std::setw(16) << hash;
    return oss.str();
}

std::string TaskMemoBuilder::ComputeFirstMsgKey(const json& messages)
{
    if (!messages.is_array() || messages.empty())
        return "";

    std::vector<const json*> non_system;
    for (const auto& msg : messages) {
        const std::string role = msg.value("role", "");
        if (role != "system" && role != "developer")
            non_system.push_back(&msg);
    }
    if (non_system.empty() || non_system[0]->value("role", "") != "user")
        return "";

    uint64_t h = 14695981039346656037ULL;
    const uint64_t p = 1099511628211ULL;
    auto hash_string = [&](const std::string& s) {
        for (unsigned char c : s) { h ^= c; h *= p; }
        h ^= 0xFF; h *= p;
    };
    hash_string("user");
    std::string content_str;
    if (non_system[0]->contains("content")) {
        content_str = (*non_system[0])["content"].is_string()
            ? (*non_system[0])["content"].get<std::string>()
            : (*non_system[0])["content"].dump();
    }
    hash_string(content_str);

    std::ostringstream oss;
    oss << "tmfmk_" << std::hex << std::setfill('0') << std::setw(16) << h;
    return oss.str();
}

// ── Lookup ────────────────────────────────────────────────────────────────────

std::optional<json> TaskMemoBuilder::Lookup(const json& raw_messages) const
{
    if (!config_.enabled || !store_)
        return std::nullopt;
    return store_->Lookup(ComputeFingerprint(raw_messages), ComputeFirstMsgKey(raw_messages));
}

// ── BuildRuleLayer ────────────────────────────────────────────────────────────

namespace {
    constexpr size_t kMaxItemsPerField = 20;

    void AppendCapped(json& field, const std::string& item, size_t cap) {
        if (!field.is_array())
            field = json::array();
        for (const auto& existing : field) {
            if (existing.is_string() && existing.get<std::string>() == item)
                return;
        }
        field.push_back(item);
        while (field.size() > cap)
            field.erase(field.begin());
    }

    bool HasNonEmptyErrorField(const std::string& content, const char* key) {
        size_t pos = 0;
        while ((pos = content.find(key, pos)) != std::string::npos) {
            size_t cursor = pos + strlen(key);
            while (cursor < content.size() && (content[cursor] == ' ' || content[cursor] == '\t'))
                cursor++;
            if (cursor < content.size() && content[cursor] == ':') {
                cursor++;
                while (cursor < content.size() && (content[cursor] == ' ' || content[cursor] == '\t'))
                    cursor++;
                static const char* kFalsy[] = { "null", "false", "0", "\"\"" };
                bool is_falsy = false;
                for (const char* f : kFalsy) {
                    size_t flen = strlen(f);
                    if (content.compare(cursor, flen, f) == 0)
                        is_falsy = true;
                }
                if (!is_falsy)
                    return true;
            }
            pos += strlen(key);
        }
        return false;
    }

    bool LooksLikeFailure(const std::string& content) {
        static const char* kSignals[] = {
            "Error:", "ERROR:", "Exception:", "Traceback (most recent call last)",
            "No such file or directory",
            "Permission denied", "command not found", "is not recognized as",
            "Failed to", "failed to", "Failed:",
        };
        for (const char* sig : kSignals) {
            if (content.find(sig) != std::string::npos)
                return true;
        }
        if (HasNonEmptyErrorField(content, "\"error\"") || HasNonEmptyErrorField(content, "\"is_error\""))
            return true;
        return false;
    }

    void CapObjectSize(json& obj, size_t cap) {
        if (!obj.is_object())
            return;
        while (obj.size() > cap)
            obj.erase(obj.begin());
    }

    bool IsReadLikeToolName(const std::string& name) {
        std::string lower = name;
        std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char c) { return (char)tolower(c); });
        // exec 类工具（即便名字含 "read"，如某些 shell 别名）不计入，由调用方对 exec 结果
        // 走正常的失败/成功摘要路径，不参与"已读文件索引"。
        return lower.find("read") != std::string::npos && lower.find("exec") == std::string::npos;
    }

    // 从承载 tool_call 的 assistant 消息文本里抽取 read 类工具调用的路径参数；兼容 Harmony
    // （to=functions.NAME ... {"path":"..."}）与 general 格式（<tool_call>{"name":"NAME",
    // "arguments":{"path":"..."}}</tool_call>）两种拼接方式。只用子串朴素扫描，不引入 <regex>，
    // 与本文件其余解析风格一致；误判的代价只是漏报/多报一次提示，不影响正确性。
    std::string ExtractReadToolPathImpl(const std::string& assistant_content) {
        std::string name;
        size_t search_from = 0;
        size_t name_pos = assistant_content.find("to=functions.");
        if (name_pos != std::string::npos) {
            size_t start = name_pos + strlen("to=functions.");
            size_t end = start;
            while (end < assistant_content.size() &&
                   (isalnum((unsigned char) assistant_content[end]) || assistant_content[end] == '_'))
                ++end;
            name = assistant_content.substr(start, end - start);
            search_from = end;
        } else {
            size_t np = assistant_content.find("\"name\"");
            if (np != std::string::npos) {
                size_t colon = assistant_content.find(':', np);
                size_t q1 = (colon == std::string::npos) ? std::string::npos : assistant_content.find('"', colon + 1);
                size_t q2 = (q1 == std::string::npos) ? std::string::npos : assistant_content.find('"', q1 + 1);
                if (q1 != std::string::npos && q2 != std::string::npos) {
                    name = assistant_content.substr(q1 + 1, q2 - q1 - 1);
                    search_from = q2;
                }
            }
        }
        if (name.empty() || !IsReadLikeToolName(name))
            return "";

        static const char* kPathKeys[] = { "\"path\"", "\"file\"", "\"file_path\"", "\"filename\"" };
        for (const char* key : kPathKeys) {
            size_t kp = assistant_content.find(key, search_from);
            if (kp == std::string::npos)
                continue;
            size_t colon = assistant_content.find(':', kp);
            size_t q1 = (colon == std::string::npos) ? std::string::npos : assistant_content.find('"', colon + 1);
            size_t q2 = (q1 == std::string::npos) ? std::string::npos : assistant_content.find('"', q1 + 1);
            if (q1 != std::string::npos && q2 != std::string::npos)
                return assistant_content.substr(q1 + 1, q2 - q1 - 1);
        }
        return "";
    }

    std::string ReplaceInvalidUtf8(const std::string& s) {
        std::string out;
        out.reserve(s.size());
        const size_t n = s.size();
        size_t i = 0;
        while (i < n) {
            const unsigned char c = static_cast<unsigned char>(s[i]);
            size_t len = c < 0x80 ? 1 : (c >= 0xC2 && c <= 0xDF) ? 2 : (c >= 0xE0 && c <= 0xEF) ? 3 : (c >= 0xF0 && c <= 0xF4) ? 4 : 0;
            bool ok = len != 0 && i + len <= n;
            for (size_t k = 1; ok && k < len; ++k)
                ok = (static_cast<unsigned char>(s[i + k]) & 0xC0) == 0x80;
            if (ok) {
                out.append(s, i, len);
                i += len;
            } else {
                out += '?';
                ++i;
            }
        }
        return out;
    }

    std::string ExtractKeyFacts(const std::string& content, size_t max_len) {
        if (LooksLikeFailure(content)) {
            size_t nl = content.find('\n');
            std::string first_line = (nl == std::string::npos) ? content : content.substr(0, nl);
            return safe_utf8_truncate(first_line, max_len, "...");
        }

        std::vector<std::string> facts;
        try {
            json parsed = json::parse(content, nullptr, false);
            if (parsed.is_object()) {
                for (auto it = parsed.begin(); it != parsed.end(); ++it) {
                    if (facts.size() >= 4) break;
                    std::string v = it.value().is_string() ? it.value().get<std::string>() : it.value().dump();
                    facts.push_back(it.key() + "=" + v);
                }
            }
        } catch (...) {}

        if (facts.empty()) {
            static const char* kPathHints[] = { "path\":", "file\":", "C:\\", "/home/", "/root/", "/mnt/", "/usr/" };
            for (const char* hint : kPathHints) {
                size_t pos = content.find(hint);
                if (pos != std::string::npos) {
                    size_t start = utf8_align_start(content, (pos > 20) ? pos - 20 : 0);
                    facts.push_back(safe_utf8_truncate(content.substr(start), 80, ""));
                    break;
                }
            }
        }

        std::string out;
        if (!facts.empty()) {
            for (const auto& f : facts) {
                if (!out.empty()) out += "; ";
                out += f;
            }
        } else {
            out = content;
        }
        return safe_utf8_truncate(out, max_len, "...");
    }
}

std::string TaskMemoBuilder::ExtractReadToolPath(const std::string& assistant_content)
{
    return ExtractReadToolPathImpl(assistant_content);
}

bool TaskMemoBuilder::LooksLikeToolFailure(const std::string& content)
{
    return LooksLikeFailure(content);
}

json TaskMemoBuilder::BuildRuleLayer(const json& prev, const std::vector<GenieChatMessage>& dropped_messages) const
{
    json entry = prev.is_object() ? prev : json::object();
    if (!entry.contains("current_plan") || !entry["current_plan"].is_string())
        entry["current_plan"] = "";
    if (!entry.contains("next_actions") || !entry["next_actions"].is_array())
        entry["next_actions"] = json::array();
    if (!entry.contains("open_questions") || !entry["open_questions"].is_array())
        entry["open_questions"] = json::array();
    if (!entry.contains("tool_state") || !entry["tool_state"].is_object())
        entry["tool_state"] = json::object();

    if (!entry.contains("read_files") || !entry["read_files"].is_object())
        entry["read_files"] = json::object();

    size_t tool_seq = entry["tool_state"].size();
    std::string last_assistant_content;
    for (const auto& m : dropped_messages) {
        std::string preview = ReplaceInvalidUtf8(ExtractKeyFacts(m.content, config_.rule_layer_preview_chars));
        if (m.role == "user") {
            AppendCapped(entry["facts_constraints"], "用户提及: " + preview, kMaxItemsPerField);
            size_t todo_pos = m.content.find("TODO");
            if (todo_pos == std::string::npos)
                todo_pos = m.content.find("todo");
            if (todo_pos != std::string::npos)
                AppendCapped(entry["open_questions"], "待办: " + ReplaceInvalidUtf8(safe_utf8_truncate(m.content.substr(todo_pos), 160, "")), kMaxItemsPerField);
        } else if (m.role == "assistant") {
            bool is_tool_call = m.content.find("to=functions.") != std::string::npos ||
                                 m.content.find("<tool_call>") != std::string::npos;
            if (is_tool_call)
                AppendCapped(entry["completed"], "已发起工具调用: " + preview, kMaxItemsPerField);
            last_assistant_content = m.content;
        } else if (m.role == "tool") {
            bool failed = LooksLikeFailure(m.content);
            entry["tool_state"]["tool_result_" + std::to_string(tool_seq++)] =
                (failed ? "失败: " : "成功: ") + preview;
            CapObjectSize(entry["tool_state"], kMaxItemsPerField);
            AppendCapped(entry["completed"], (failed ? "工具调用失败: " : "工具调用完成: ") + preview, kMaxItemsPerField);

            // 已读文件索引：被驱逐的 read 类工具结果不再丢弃全部痕迹，而是在 Task Memo 里
            // 留一条「路径 + 关键事实摘要」，供模型在原始内容已不在上下文时判断是否要重读。
            if (!failed) {
                std::string read_path = ExtractReadToolPathImpl(last_assistant_content);
                if (!read_path.empty()) {
                    entry["read_files"][read_path] = preview;
                    CapObjectSize(entry["read_files"], kMaxItemsPerField);
                }
            }
        }
    }

    size_t folded_before = entry.value("_folded_count", (size_t)0);
    size_t folded = folded_before + dropped_messages.size();
    entry["_folded_count"] = folded;
    entry["source_range"] = "messages[0:" + std::to_string(folded) + ")";

    // 轻量分页目录：每次 Update() 调用（即一次驱逐批次）分配一个递增 page_id，只记
    // {page_id, source_range, 一行确定性类别统计 gist}，不含任何原始消息文本——与
    // 现有安全脱敏边界保持一致。环形缓冲超过 page_directory.max_entries 时 FIFO
    // 淘汰最旧条目（不是按 page_id 淘汰，是按数组顺序，天然等价于最旧）。
    {
        size_t tool_failed = 0;
        for (const auto& m : dropped_messages) {
            if (m.role == "tool" && LooksLikeFailure(m.content))
                ++tool_failed;
        }
        size_t page_id = entry.value("_next_page_id", (size_t) 1);
        entry["_next_page_id"] = page_id + 1;

        json page = json::object();
        page["page_id"] = page_id;
        page["source_range"] = "messages[" + std::to_string(folded_before) + ":" + std::to_string(folded) + ")";
        std::ostringstream gist;
        gist << dropped_messages.size() << " 条消息";
        if (tool_failed > 0)
            gist << "，含 " << tool_failed << " 条工具失败";
        page["gist"] = gist.str();

        if (!entry.contains("pages") || !entry["pages"].is_array())
            entry["pages"] = json::array();
        entry["pages"].push_back(page);
        while (entry["pages"].size() > config_.page_directory.max_entries)
            entry["pages"].erase(entry["pages"].begin());
    }

    std::string concat;
    for (const auto& m : dropped_messages)
        concat += m.role + "|" + m.content;
    entry["content_hash"] = std::to_string(std::hash<std::string>{}(concat));
    entry["confidence"] = 0.6;
    return entry;
}

// ── ExtractGoalAnchor（原始目标锚点鲁棒抓取）───────────────────────────────────

std::pair<std::string, bool> TaskMemoBuilder::ExtractGoalAnchor(const json& raw_messages) const
{
    if (!raw_messages.is_array() || raw_messages.empty())
        return {"", false};

    std::vector<std::string> candidates;
    for (const auto& msg : raw_messages) {
        if (msg.value("role", "") != "user")
            continue;
        // 用 get_json_value<std::string>() 而非裸 dump()：content 既可能是纯字符串，
        // 也可能是 OpenAI 多段 content-parts 数组，该函数对两种格式都能安全提取纯文本。
        candidates.push_back(get_json_value<std::string>(msg, "content", ""));
        if (candidates.size() >= config_.goal_scan_window)
            break;
    }
    if (candidates.empty())
        return {"", false};

    for (const auto& c : candidates) {
        if (c.size() >= config_.min_goal_signal_chars)
            return {safe_utf8_truncate(c, config_.goal_anchor_preview_chars, ""), true};
    }
    // 全部不达标（如首几条均为"你好"一类寒暄）：仍需确定性兜底，取第一条并标记低置信度，
    // 不能返回空锚点——这是"不遗忘目标"硬保证的前提，哪怕信号很弱也要有锚点可渲染。
    return {safe_utf8_truncate(candidates[0], config_.goal_anchor_preview_chars, ""), false};
}

// ── ShouldTriggerModelLayer ───────────────────────────────────────────────────

bool TaskMemoBuilder::ShouldTriggerModelLayer(const std::vector<GenieChatMessage>& dropped_messages,
                                               bool has_prev, double prev_confidence) const
{
    if (!config_.model_layer_enabled || !infer_fn_)
        return false;
    if (!has_prev)
        return true;
    if (prev_confidence < config_.low_confidence_threshold)
        return true;

    size_t max_tool_run = 0, cur_run = 0;
    bool has_error = false;
    for (const auto& m : dropped_messages) {
        if (m.role == "tool") { ++cur_run; max_tool_run = std::max(max_tool_run, cur_run); }
        else cur_run = 0;
        if (LooksLikeFailure(m.content))
            has_error = true;
    }
    return max_tool_run >= config_.long_tool_chain_threshold || has_error;
}

// ── BuildModelPrompt（沿用 LongTextSummarizer::BuildMapPrompt 的 Harmony/General 双格式约定）──

std::string TaskMemoBuilder::BuildModelPrompt(const json& prev, const json& rule_layer_result) const
{
    std::ostringstream task;
    task << "Maintain a structured task memo for a long-running session. Update it using the "
         << "rule-extracted facts below. Output ONLY one JSON object with exactly these fields: "
         << "completed (string array), current_plan (string), next_actions (string array), "
         << "facts_constraints (string array), open_questions (string array), tool_state (object), "
         << "confidence (number 0.0-1.0), goal_restatement (string, OPTIONAL: a one-sentence "
         << "restatement of the user's original goal, only if it can be confidently inferred from "
         << "the context; omit this field entirely if unclear, do not guess). "
         << "No prose, no markdown fences, JSON only.\n\n"
         << "Previous memo: " << (prev.is_object() ? prev.dump() : std::string("{}")) << "\n\n"
         << "Rule-extracted facts: " << rule_layer_result.dump();

    std::string task_str = task.str();
    bool is_harmony = (instance_config_.get_prompt_type() == PromptType::Harmony);

    if (is_harmony) {
        std::string prompt;
        prompt += "<|start|>system<|message|>You are a task-memo maintenance assistant.<|end|>";
        prompt += "<|start|>user<|message|>" + task_str + "<|end|>";
        prompt += "<|start|>assistant<|channel|>final<|message|>";
        return prompt;
    }

    const auto& j = instance_config_.get_prompt_template();
    if (j.is_object() && j.contains("system") && j["system"].is_string()
            && j.contains("user") && j["user"].is_string()
            && j.contains("start") && j["start"].is_string()) {
        std::string system_instruction = "You are a task-memo maintenance assistant.";
        if (instance_config_.is_thinking_model())
            system_instruction += "/no_think";
        return str_replace(j["system"].get<std::string>(), "string", system_instruction)
             + str_replace(j["user"].get<std::string>(), "string", task_str)
             + j["start"].get<std::string>();
    }
    return task_str;
}

// ── TryParseModelOutput ───────────────────────────────────────────────────────

bool TaskMemoBuilder::TryParseModelOutput(const std::string& raw, json& out) const
{
    size_t start = raw.find('{');
    size_t end = raw.rfind('}');
    if (start == std::string::npos || end == std::string::npos || end <= start)
        return false;
    try {
        out = json::parse(raw.substr(start, end - start + 1));
        return out.is_object();
    } catch (...) {
        return false;
    }
}

// ── Update ────────────────────────────────────────────────────────────────────

TaskMemoBuilder::UpdateResult TaskMemoBuilder::Update(const json& raw_messages,
                                                       const std::vector<GenieChatMessage>& dropped_messages)
{
    UpdateResult res;
    size_t min_trigger = std::max<size_t>(1, config_.min_dropped_for_trigger);
    if (!config_.enabled || !store_ || dropped_messages.size() < min_trigger)
        return res;

    std::string fingerprint = ComputeFingerprint(raw_messages);
    std::string first_msg_key = ComputeFirstMsgKey(raw_messages);
    std::optional<json> prev = store_->Lookup(fingerprint, first_msg_key);
    bool has_prev = prev.has_value();
    double prev_confidence = has_prev ? prev->value("confidence", 0.0) : 0.0;
    size_t prev_refresh = has_prev ? prev->value("refresh_count", (size_t)0) : 0;

    size_t already_folded = has_prev ? prev->value("_folded_count", (size_t)0) : 0;
    if (already_folded > dropped_messages.size())
        already_folded = 0;
    if (has_prev && already_folded == dropped_messages.size()) {
        res.active = true;
        res.confidence = prev_confidence;
        res.refresh_count = prev_refresh;
        res.pages_total = (prev->contains("pages") && (*prev)["pages"].is_array()) ? (*prev)["pages"].size() : 0;
        res.compact_render = RenderCompact(*prev);
        return res;
    }
    const std::vector<GenieChatMessage> new_dropped(dropped_messages.begin() + (long long) already_folded,
                                                    dropped_messages.end());

    json entry = BuildRuleLayer(has_prev ? *prev : json::object(), new_dropped);

    // 原始目标锚点：只在尚未捕获过时抓取一次；一旦捕获，后续轮次通过 entry/prev 透传，
    // 永不被规则层或模型层的后续更新覆盖（BuildRuleLayer 已经把 prev 的既有字段原样
    // 带入 entry，这里只需补齐"从未捕获过"的情形）。
    bool goal_already_captured = entry.contains("original_goal_raw")
        && entry["original_goal_raw"].is_string()
        && !entry["original_goal_raw"].get<std::string>().empty();
    if (!goal_already_captured) {
        auto anchor = ExtractGoalAnchor(raw_messages);
        entry["original_goal_raw"] = anchor.first;
        entry["original_goal_confidence"] = anchor.second ? std::string("high") : std::string("low");
    }

    if (ShouldTriggerModelLayer(new_dropped, has_prev, prev_confidence)
            && (!is_alive_fn_ || is_alive_fn_())) {
        std::string prompt = BuildModelPrompt(has_prev ? *prev : json::object(), entry);
        std::string raw_output = infer_fn_(prompt);
        json model_entry;
        if (!raw_output.empty() && TryParseModelOutput(raw_output, model_entry)) {
            model_entry["source_range"] = entry["source_range"];
            model_entry["content_hash"] = entry["content_hash"];
            model_entry["_folded_count"] = entry["_folded_count"];
            // pages/_next_page_id 是规则层维护的轻量分页目录，模型层不感知、不生成，
            // 必须显式透传，否则模型层触发时这批 Update() 新增的分页记录会被整体丢失。
            model_entry["pages"] = entry["pages"];
            model_entry["_next_page_id"] = entry["_next_page_id"];
            // read_files（已读文件索引）同理：模型层不感知、不生成，必须显式透传。
            model_entry["read_files"] = entry["read_files"];
            // original_goal_raw/confidence 权威兜底，从 entry 显式透传，不允许模型层输出覆盖。
            model_entry["original_goal_raw"] = entry["original_goal_raw"];
            model_entry["original_goal_confidence"] = entry["original_goal_confidence"];
            // goal_restatement 是模型层旁路产出的辅助字段，写入 original_goal_refined 并标注
            // "可能不准确"；模型本轮未给出时，沿用 entry（即 prev）里已有的上一次旁路结果。
            if (model_entry.contains("goal_restatement") && model_entry["goal_restatement"].is_string()
                    && !model_entry["goal_restatement"].get<std::string>().empty()) {
                model_entry["original_goal_refined"] =
                    safe_utf8_truncate(model_entry["goal_restatement"].get<std::string>(),
                                        config_.goal_anchor_preview_chars, "");
            } else if (entry.contains("original_goal_refined")) {
                model_entry["original_goal_refined"] = entry["original_goal_refined"];
            }
            model_entry.erase("goal_restatement");
            model_entry["confidence"] = std::min(1.0, std::max(0.0, model_entry.value("confidence", 0.75)));
            entry = model_entry;
        }
    }

    // 连续失败不轻易放弃：与 ToolCallCircuitBreakerStore 打通，只读查询（不调用
    // RecordLayer3Trigger/RecordSuccess，保持该 store 计数语义的唯一写入方仍是
    // response_dispatcher.cpp）。key 计算方式与该 store 自身注释里记录的约定一致：
    // session_key 用 ComputeFirstMsgKey（已算好的 first_msg_key），model_name 用
    // instance_config_.get_model_name()，与 response_dispatcher.cpp 查到的是同一条记录。
    std::string cb_key = ToolCallCircuitBreakerStore::MakeKey(first_msg_key, instance_config_.get_model_name());
    entry["failure_streak"] = ToolCallCircuitBreakerStore::GetInstance().GetConsecutiveCount(cb_key);
    // Render() 是 static 方法读不到 config_，阈值随 entry 一起落盘，保持 Render() 签名
    // 不变（纯函数于 entry），也让每条已存储的备忘录自描述当时生效的阈值是多少。
    entry["failure_streak_warn_threshold"] = config_.failure_streak_warn_threshold;

    const auto &redundant_cfg = instance_config_.i_model_config_.GetToolCallRepairConfig().redundant_call_guard;
    if (redundant_cfg.enabled) {
        std::string rc_key = ToolCallRepetitionStore::MakeKey(first_msg_key, instance_config_.get_model_name());
        entry["redundant_tool_call_count"] = ToolCallRepetitionStore::GetInstance().GetRepeatCount(rc_key);
        entry["redundant_tool_call_threshold"] = redundant_cfg.repeat_threshold;
    } else {
        entry["redundant_tool_call_count"] = 0;
        entry["redundant_tool_call_threshold"] = 0;
    }

    entry["refresh_count"] = prev_refresh + 1;
    store_->Put(fingerprint, first_msg_key, entry);

    res.active = true;
    res.confidence = entry.value("confidence", 0.0);
    res.refresh_count = entry.value("refresh_count", (size_t)0);
    res.pages_total = (entry.contains("pages") && entry["pages"].is_array()) ? entry["pages"].size() : 0;
    res.compact_render = RenderCompact(entry);
    return res;
}

// ── Render / RenderCompact ────────────────────────────────────────────────────

std::string TaskMemoBuilder::Render(const json& entry, size_t max_chars)
{
    if (!entry.is_object())
        return "";

    auto render_list = [&](const char* label, const char* key) -> std::string {
        if (!entry.contains(key) || !entry[key].is_array() || entry[key].empty())
            return "";
        std::ostringstream oss;
        oss << label << ":\n";
        for (const auto& item : entry[key]) {
            if (item.is_string())
                oss << "- " << item.get<std::string>() << "\n";
        }
        return oss.str();
    };

    std::string header = "## Task Memo\n";

    // 目标锚点块：最高优先级，不参与下面的"整块跳过"循环；预算不足时只截断该块文本
    // 本身（复用 safe_utf8_truncate），绝不整块跳过——这是"不遗忘目标"的硬保证。
    // 连续失败持久化提示行同属这一不可整块丢弃的段落，与目标锚点块拼在一起。
    std::string goal_block;
    std::string goal_raw = entry.value("original_goal_raw", std::string(""));
    if (!goal_raw.empty()) {
        std::ostringstream oss;
        oss << "Original goal: " << goal_raw;
        if (entry.value("original_goal_confidence", std::string("high")) == "low")
            oss << " (low confidence: no strong signal detected in the opening messages)";
        oss << "\n";
        std::string goal_refined = entry.value("original_goal_refined", std::string(""));
        if (!goal_refined.empty()) {
            oss << "Model-restated goal (may be inaccurate; the raw goal above is authoritative): "
                << goal_refined << "\n";
        }
        goal_block = oss.str();
    }

    int failure_streak = entry.value("failure_streak", 0);
    int failure_streak_warn_threshold = entry.value("failure_streak_warn_threshold", 0);
    if (failure_streak_warn_threshold > 0 && failure_streak >= failure_streak_warn_threshold) {
        std::ostringstream oss;
        oss << "Note: " << failure_streak << " consecutive tool-call failures detected. "
            << "Try an alternative approach instead of giving up on the original goal above.\n";
        goal_block += oss.str();
    }

    int redundant_tool_call_count = entry.value("redundant_tool_call_count", 0);
    int redundant_tool_call_threshold = entry.value("redundant_tool_call_threshold", 0);
    if (redundant_tool_call_threshold > 0 && redundant_tool_call_count >= redundant_tool_call_threshold) {
        goal_block += TaskMemoBuilder::RenderRedundantToolCallNote(redundant_tool_call_count);
    }

    std::string plan = entry.value("current_plan", std::string(""));
    if (!plan.empty())
        header += "Current plan: " + plan + "\n";

    std::string tool_state_block;
    if (entry.contains("tool_state") && entry["tool_state"].is_object() && !entry["tool_state"].empty()) {
        std::ostringstream oss;
        oss << "Tool state:\n";
        for (auto it = entry["tool_state"].begin(); it != entry["tool_state"].end(); ++it) {
            oss << "- " << it.key() << ": "
                << (it.value().is_string() ? it.value().get<std::string>() : it.value().dump()) << "\n";
        }
        tool_state_block = oss.str();
    }

    // 已读文件索引：被驱逐的 read 类工具结果不再丢弃全部痕迹，模型重复 read 同一路径前
    // 可以先看这里判断是否真的需要重读；仅记路径与关键事实摘要，不含原始文件内容。
    std::string read_files_block;
    if (entry.contains("read_files") && entry["read_files"].is_object() && !entry["read_files"].empty()) {
        std::ostringstream oss;
        oss << "Files already read, content evicted from context (re-reading the same call "
               "returns the full content again; summary below is for reference only):\n";
        for (auto it = entry["read_files"].begin(); it != entry["read_files"].end(); ++it) {
            oss << "- " << it.key() << ": "
                << (it.value().is_string() ? it.value().get<std::string>() : it.value().dump()) << "\n";
        }
        read_files_block = oss.str();
    }

    // 轻量分页目录：最低优先级，预算放不下时整块跳过。只渲染 page_id/source_range/
    // gist 三个字段，不含任何原始消息文本——与 BuildRuleLayer() 落盘时的安全边界一致。
    std::string page_directory_block;
    if (entry.contains("pages") && entry["pages"].is_array() && !entry["pages"].empty()) {
        std::ostringstream oss;
        oss << "Evicted message pages (oldest first, no original text retained):\n";
        for (const auto& page : entry["pages"]) {
            if (!page.is_object())
                continue;
            oss << "- page " << page.value("page_id", (size_t) 0) << " ("
                << page.value("source_range", std::string("")) << "): "
                << page.value("gist", std::string("")) << "\n";
        }
        page_directory_block = oss.str();
    }

    // 由高到低优先级排列；预算不足时跳过放不下的块，不做块内截断。
    const std::string blocks[] = {
        render_list("Completed", "completed"),
        render_list("Next actions", "next_actions"),
        render_list("Facts / constraints", "facts_constraints"),
        render_list("Open questions", "open_questions"),
        read_files_block,
        tool_state_block,
        page_directory_block,
    };
    static const char* kBlockNames[] = {
        "completed", "next_actions", "facts_constraints", "open_questions",
        "read_files", "tool_state", "page_directory",
    };

    std::string out = header;
    if (!goal_block.empty()) {
        if (max_chars > 0 && out.size() + goal_block.size() > max_chars) {
            // 预算不足：只截断目标锚点块文本本身，绝不整块丢弃。
            size_t remaining = (max_chars > out.size()) ? (max_chars - out.size()) : 0;
            out += safe_utf8_truncate(goal_block, remaining, "");
        } else {
            out += goal_block;
        }
    }
    size_t block_index = 0;
    for (const auto& block : blocks) {
        const char* block_name = kBlockNames[block_index++];
        if (block.empty())
            continue;
        if (max_chars > 0 && out.size() + block.size() > max_chars) {
            My_Log{My_Log::Level::kWarning}
                << "[TaskMemo] Render block skipped: name=" << block_name
                << ", block_chars=" << block.size()
                << ", used_chars=" << out.size()
                << ", max_chars=" << max_chars << std::endl;
            continue;
        }
        out += block;
    }
    if (max_chars > 0 && out.size() > max_chars)
        out = safe_utf8_truncate(out, max_chars, "");
    return out;
}

std::string TaskMemoBuilder::RenderRedundantToolCallNote(int repeat_count)
{
    std::ostringstream oss;
    oss << "Note: you have called the same tool with the same arguments " << repeat_count
        << " time(s) in a row. If that earlier result is still visible above, re-reading it won't "
           "give new information; use what you already have. If it's no longer visible (evicted "
           "from context), calling it again will return the full content.\n";
    return oss.str();
}

std::string TaskMemoBuilder::RenderCompact(const json& entry)
{
    if (!entry.is_object())
        return "";

    size_t completed_n = (entry.contains("completed") && entry["completed"].is_array())
        ? entry["completed"].size() : 0;
    std::string plan = entry.value("current_plan", std::string(""));
    std::string next;
    if (entry.contains("next_actions") && entry["next_actions"].is_array() && !entry["next_actions"].empty()
            && entry["next_actions"][0].is_string()) {
        next = entry["next_actions"][0].get<std::string>();
    }

    std::ostringstream oss;
    oss << "[Task memo: " << completed_n << " step(s) completed";
    std::string goal_raw = entry.value("original_goal_raw", std::string(""));
    if (!goal_raw.empty())
        oss << "; goal: " << safe_utf8_truncate(goal_raw, 80, "...");
    if (!plan.empty())
        oss << "; plan: " << plan;
    if (!next.empty())
        oss << "; next: " << next;
    oss << "]\n";
    return oss.str();
}
