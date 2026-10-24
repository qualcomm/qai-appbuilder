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
#include <cctype>
#include <chrono>
#include <mutex>
#include <unordered_map>

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
    constexpr size_t kMaxHardRules = 12;
    constexpr size_t kMaxHardRulesPerFile = 6;
    constexpr size_t kMaxHardRuleChars = 220;
    constexpr size_t kMaxArtifacts = 8;
    constexpr size_t kMaxArtifactLinesPerResult = 3;
    constexpr size_t kMaxLastSuccessChars = 900;
    // 已尝试查询索引：资源数、每个资源下保留的查询条数上限，避免长链路里自身无限增长。
    constexpr size_t kMaxTriedQueryResources = 10;
    constexpr size_t kMaxTriedQueriesPerResource = 6;
    constexpr size_t kMaxCommandResourceKeyChars = 120;

    std::unordered_map<size_t, std::string> BuildToolCallContentByRawIndex(const json& raw_messages)
    {
        std::unordered_map<std::string, std::string> content_by_id;
        if (!raw_messages.is_array())
            return {};

        for (size_t i = 0; i < raw_messages.size(); ++i) {
            const auto& message = raw_messages[i];
            if (!message.is_object() || message.value("role", "") != "assistant"
                    || !message.contains("tool_calls") || !message["tool_calls"].is_array())
                continue;
            for (const auto& tool_call : message["tool_calls"]) {
                if (!tool_call.is_object() || !tool_call.contains("id") || !tool_call["id"].is_string())
                    continue;
                std::string name;
                std::string arguments;
                if (tool_call.contains("function") && tool_call["function"].is_object()) {
                    const auto& function = tool_call["function"];
                    name = function.value("name", "");
                    if (function.contains("arguments") && !function["arguments"].is_null())
                        arguments = function["arguments"].is_string()
                            ? function["arguments"].get<std::string>() : function["arguments"].dump();
                } else {
                    name = tool_call.value("name", "");
                    if (tool_call.contains("arguments") && !tool_call["arguments"].is_null())
                        arguments = tool_call["arguments"].is_string()
                            ? tool_call["arguments"].get<std::string>() : tool_call["arguments"].dump();
                }
                if (name.empty())
                    continue;
                if (arguments.empty())
                    arguments = "{}";
                content_by_id[tool_call["id"].get<std::string>()] =
                    "<tool_call>\n{\"name\": \"" + name + "\", \"arguments\": " + arguments +
                    "}\n</tool_call>";
            }
        }

        std::unordered_map<size_t, std::string> result;
        for (size_t i = 0; i < raw_messages.size(); ++i) {
            const auto& message = raw_messages[i];
            if (!message.is_object() || message.value("role", "") != "tool"
                    || !message.contains("tool_call_id") || !message["tool_call_id"].is_string())
                continue;
            auto it = content_by_id.find(message["tool_call_id"].get<std::string>());
            if (it != content_by_id.end())
                result[i] = it->second;
        }
        return result;
    }

    bool MatchesDroppedToolContent(const std::string& raw_content, const std::string& dropped_content)
    {
        return raw_content == dropped_content
            || "<tool_response>\n" + raw_content + "\n</tool_response>\n" == dropped_content;
    }

    std::vector<std::string> MatchDroppedToolCallContent(
        const json& raw_messages, const std::vector<GenieChatMessage>& dropped_messages)
    {
        const auto content_by_raw_index = BuildToolCallContentByRawIndex(raw_messages);
        std::vector<std::string> result(dropped_messages.size());
        size_t raw_cursor = 0;
        for (size_t i = 0; i < dropped_messages.size(); ++i) {
            if (dropped_messages[i].role != "tool")
                continue;
            bool matched = false;
            while (raw_cursor < raw_messages.size()) {
                const size_t raw_index = raw_cursor++;
                const auto& raw_message = raw_messages[raw_index];
                if (!raw_message.is_object() || raw_message.value("role", "") != "tool"
                        || !raw_message.contains("content") || !raw_message["content"].is_string()
                        || !MatchesDroppedToolContent(raw_message["content"].get<std::string>(),
                                                       dropped_messages[i].content))
                    continue;
                auto it = content_by_raw_index.find(raw_index);
                if (it != content_by_raw_index.end())
                    result[i] = it->second;
                matched = true;
                break;
            }
            if (!matched)
                raw_cursor = raw_messages.size();
        }
        return result;
    }

    std::string FullToolCall(const std::string& assistant_content) {
        std::string visible = TaskMemoBuilder::StripThinkBlock(assistant_content);
        const size_t tc = visible.find("<tool_call>");
        if (tc == std::string::npos)
            return "";
        const size_t end = visible.find("</tool_call>", tc);
        std::string body = visible.substr(tc + 11, end == std::string::npos ? std::string::npos : end - tc - 11);
        try {
            json j = json::parse(body);
            return j.value("name", "") + " " + (j.contains("arguments") ? j["arguments"].dump() : std::string("{}"));
        } catch (...) {
            for (char& c : body)
                if (c == '\n' || c == '\r') c = ' ';
            return body;
        }
    }

    std::vector<std::string> ExtractArtifactLines(const std::string& content) {
        static const char* kVerbs[] = {"saved", "written", "created", "generated", "exported", "wrote", "已保存", "已生成"};
        std::vector<std::string> out;
        size_t pos = 0;
        while (pos < content.size() && out.size() < kMaxArtifactLinesPerResult) {
            size_t nl = content.find('\n', pos);
            if (nl == std::string::npos) nl = content.size();
            std::string line = content.substr(pos, nl - pos);
            pos = nl + 1;
            std::string lower = line;
            std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char c) { return (char) tolower(c); });
            bool verb = false;
            for (const char* v : kVerbs)
                if (lower.find(v) != std::string::npos) { verb = true; break; }
            const bool path = line.find(":\\") != std::string::npos || line.find(":/") != std::string::npos ||
                              line.find(" /") != std::string::npos;
            if (!verb || !path)
                continue;
            const size_t b = line.find_first_not_of(" \t\r");
            line = line.substr(b == std::string::npos ? 0 : b);
            while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
            out.push_back(safe_utf8_truncate(line, kMaxHardRuleChars, "..."));
        }
        return out;
    }

    bool IsHardRuleLine(const std::string& line) {
        static const char* kMarkers[] = {"NEVER", "MUST", "MANDATORY", "Do NOT", "DO NOT",
                                         "必须", "禁止", "不得", "严禁"};
        for (const char* k : kMarkers)
            if (line.find(k) != std::string::npos) return true;
        return false;
    }

    std::vector<std::string> ExtractHardRules(const std::string& content) {
        std::vector<std::string> out;
        size_t pos = 0;
        while (pos < content.size() && out.size() < kMaxHardRulesPerFile) {
            size_t nl = content.find('\n', pos);
            if (nl == std::string::npos) nl = content.size();
            std::string line = content.substr(pos, nl - pos);
            pos = nl + 1;
            const size_t b = line.find_first_not_of(" \t\r>-*#|");
            if (b == std::string::npos) continue;
            line = line.substr(b);
            while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
            if (line.size() < 8 || !IsHardRuleLine(line)) continue;
            out.push_back(safe_utf8_truncate(line, kMaxHardRuleChars, "..."));
        }
        return out;
    }

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

    bool LineHasFailureSignal(const std::string& line) {
        static const char* kSignals[] = {
            "Error:", "ERROR:", "Exception:", "Traceback (most recent call last)",
            "No such file or directory",
            "Permission denied", "command not found", "is not recognized as",
            "Failed to", "failed to", "Failed:",
        };
        const size_t begin = line.find_first_not_of(" \t\r");
        if (begin == std::string::npos)
            return false;
        for (const char* sig : kSignals) {
            size_t pos = line.find(sig, begin);
            while (pos != std::string::npos) {
                if (pos == begin || (pos >= 2 && line.compare(pos - 2, 2, ": ") == 0))
                    return true;
                pos = line.find(sig, pos + 1);
            }
        }
        return false;
    }

    bool LooksLikeFailure(const std::string& content) {
        size_t pos = 0;
        while (pos <= content.size()) {
            const size_t nl = content.find('\n', pos);
            const size_t end = (nl == std::string::npos) ? content.size() : nl;
            if (LineHasFailureSignal(content.substr(pos, end - pos)))
                return true;
            if (nl == std::string::npos)
                break;
            pos = nl + 1;
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

    // 从承载 tool_call 的 assistant 消息文本里抽取工具名与参数扫描起点；兼容 Harmony
    // （to=functions.NAME ...）与 general 格式（<tool_call>{"name":"NAME", ...}</tool_call>）
    // 两种拼接方式。只用子串朴素扫描，不引入 <regex>，与本文件其余解析风格一致；误判的
    // 代价只是漏报/多报一次提示，不影响正确性。供 ExtractReadToolPathImpl 与
    // ExtractAnyToolPathArg 共用，避免重复实现同一段扫描逻辑。
    bool ExtractToolNameAndSearchFrom(const std::string& assistant_content, std::string& name_out, size_t& search_from_out) {
        size_t name_pos = assistant_content.find("to=functions.");
        if (name_pos != std::string::npos) {
            size_t start = name_pos + strlen("to=functions.");
            size_t end = start;
            while (end < assistant_content.size() &&
                   (isalnum((unsigned char) assistant_content[end]) || assistant_content[end] == '_'))
                ++end;
            name_out = assistant_content.substr(start, end - start);
            search_from_out = end;
            return !name_out.empty();
        }
        size_t np = assistant_content.find("\"name\"");
        if (np != std::string::npos) {
            size_t colon = assistant_content.find(':', np);
            size_t q1 = (colon == std::string::npos) ? std::string::npos : assistant_content.find('"', colon + 1);
            size_t q2 = (q1 == std::string::npos) ? std::string::npos : assistant_content.find('"', q1 + 1);
            if (q1 != std::string::npos && q2 != std::string::npos) {
                name_out = assistant_content.substr(q1 + 1, q2 - q1 - 1);
                search_from_out = q2;
                return !name_out.empty();
            }
        }
        return false;
    }

    std::string ExtractPathArgFrom(const std::string& assistant_content, size_t search_from) {
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

    std::string ExtractCommandArgFrom(const std::string& assistant_content, size_t search_from) {
        static const char* kCommandKeys[] = { "\"command\"", "\"cmd\"", "\"script\"" };
        for (const char* key : kCommandKeys) {
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

    std::string ExtractRangeSuffixFrom(const std::string& assistant_content, size_t search_from) {
        static const char* kRangeKeys[] = {
            "\"offset\"", "\"limit\"", "\"start_line\"", "\"end_line\"", "\"lines\""
        };
        std::string suffix;
        for (const char* key : kRangeKeys) {
            const size_t key_pos = assistant_content.find(key, search_from);
            if (key_pos == std::string::npos)
                continue;
            const size_t colon = assistant_content.find(':', key_pos + strlen(key));
            if (colon == std::string::npos)
                continue;
            size_t value_start = colon + 1;
            while (value_start < assistant_content.size() &&
                   std::isspace(static_cast<unsigned char>(assistant_content[value_start])))
                ++value_start;
            if (value_start >= assistant_content.size())
                continue;
            size_t value_end = value_start;
            if (assistant_content[value_start] == '"') {
                value_end = assistant_content.find('"', value_start + 1);
                if (value_end == std::string::npos)
                    continue;
                ++value_end;
            } else if (assistant_content[value_start] == '[') {
                value_end = assistant_content.find(']', value_start + 1);
                if (value_end == std::string::npos)
                    continue;
                ++value_end;
            } else {
                while (value_end < assistant_content.size() &&
                       assistant_content[value_end] != ',' && assistant_content[value_end] != '}')
                    ++value_end;
                while (value_end > value_start &&
                       std::isspace(static_cast<unsigned char>(assistant_content[value_end - 1])))
                    --value_end;
            }
            if (value_end <= value_start)
                continue;
            if (suffix.empty())
                suffix = "#range:";
            else
                suffix += ",";
            suffix += std::string(key + 1, strlen(key) - 2) + "=" +
                      assistant_content.substr(value_start, value_end - value_start);
        }
        return suffix;
    }

    std::string ExtractReadToolPathImpl(const std::string& assistant_content) {
        const std::string visible = TaskMemoBuilder::StripThinkBlock(assistant_content);
        std::string name;
        size_t search_from = 0;
        if (!ExtractToolNameAndSearchFrom(visible, name, search_from) || !IsReadLikeToolName(name))
            return "";
        const std::string path = ExtractPathArgFrom(visible, search_from);
        return path.empty() ? path : path + ExtractRangeSuffixFrom(visible, search_from);
    }

    // 已尝试查询索引专用：与 ExtractReadToolPathImpl 唯一区别是不做 IsReadLikeToolName
    // 过滤——任意工具（典型如 grep 这类按 pattern 检索的工具）只要带路径类参数就提取,
    // 不需要在这里硬编码具体工具名。
    std::string ExtractAnyToolPathArg(const std::string& assistant_content) {
        const std::string visible = TaskMemoBuilder::StripThinkBlock(assistant_content);
        std::string name;
        size_t search_from = 0;
        if (!ExtractToolNameAndSearchFrom(visible, name, search_from))
            return "";
        std::string path = ExtractPathArgFrom(visible, search_from);
        if (!path.empty())
            return path;
        std::string command = ExtractCommandArgFrom(visible, search_from);
        if (!command.empty())
            return "cmd:" + safe_utf8_truncate(command, kMaxCommandResourceKeyChars, "");
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

std::string TaskMemoBuilder::StripThinkBlock(const std::string& content)
{
    const size_t close = content.rfind("</think>");
    if (close != std::string::npos)
        return content.substr(close + 8);
    const size_t open = content.find("<think>");
    return open == std::string::npos ? content : content.substr(0, open);
}

namespace {
    const char kPinPrefix[] = "[reasoning note: ";
    std::mutex g_pin_mu;
    std::unordered_map<std::string, std::pair<std::string, std::chrono::steady_clock::time_point>> g_pins;
}

void TaskMemoBuilder::PinThinkConclusion(const std::string& session_key, const std::string& call_signature,
                                         const std::string& conclusion)
{
    if (session_key.empty() || call_signature.empty() || conclusion.empty())
        return;
    std::lock_guard<std::mutex> lk(g_pin_mu);
    const auto now = std::chrono::steady_clock::now();
    if (g_pins.size() > 4096) {
        for (auto it = g_pins.begin(); it != g_pins.end();)
            it = (now - it->second.second > std::chrono::hours(2)) ? g_pins.erase(it) : std::next(it);
        if (g_pins.size() > 4096)
            g_pins.clear();
    }
    g_pins[session_key + "\x1f" + call_signature] = {conclusion, now};
}

std::string TaskMemoBuilder::LookupPinnedConclusion(const std::string& session_key, const std::string& call_signature)
{
    if (session_key.empty() || call_signature.empty())
        return "";
    std::lock_guard<std::mutex> lk(g_pin_mu);
    auto it = g_pins.find(session_key + "\x1f" + call_signature);
    return it == g_pins.end() ? "" : std::string(kPinPrefix) + it->second.first + "]\n";
}

std::string TaskMemoBuilder::ExtractThinkConclusion(const std::string& content)
{
    if (content.rfind(kPinPrefix, 0) == 0) {
        const size_t end = content.find("]\n");
        return end == std::string::npos ? "" : content.substr(sizeof(kPinPrefix) - 1, end - (sizeof(kPinPrefix) - 1));
    }
    size_t open = content.find("<think>");
    size_t close = content.find("</think>", open == std::string::npos ? 0 : open);
    if (open == std::string::npos && close == std::string::npos)
        return "";
    const size_t body_begin = open == std::string::npos ? 0 : open + 7;
    std::string body = content.substr(body_begin, close == std::string::npos ? std::string::npos : close - body_begin);
    static const std::string kForced = "Considering the time constraints";
    const size_t forced = body.rfind(kForced);
    if (forced != std::string::npos)
        body.resize(forced);
    while (!body.empty() && std::isspace(static_cast<unsigned char>(body.back())))
        body.pop_back();
    if (body.empty())
        return "";
    size_t start = body.size() > 240 ? body.size() - 240 : 0;
    if (start > 0) {
        const size_t nl = body.find('\n', start);
        const size_t dot = body.find(". ", start);
        const size_t cut = std::min(nl, dot == std::string::npos ? std::string::npos : dot + 1);
        if (cut != std::string::npos && cut + 1 < body.size())
            start = cut + 1;
    }
    while (start < body.size() && (static_cast<unsigned char>(body[start]) & 0xC0) == 0x80)
        ++start;
    std::string out = body.substr(start);
    for (char& c : out)
        if (c == '\n' || c == '\r') c = ' ';
    return out;
}

std::string TaskMemoBuilder::ExtractToolCallSummary(const std::string& content)
{
    std::string visible = StripThinkBlock(content);
    if (visible.rfind(kPinPrefix, 0) == 0) {
        const size_t end = visible.find("]\n");
        if (end != std::string::npos)
            visible = visible.substr(end + 2);
    }
    const size_t tc = visible.find("<tool_call>");
    if (tc == std::string::npos)
        return "";
    size_t end = visible.find("</tool_call>", tc);
    std::string body = visible.substr(tc + 11, end == std::string::npos ? std::string::npos : end - tc - 11);
    try {
        json j = json::parse(body);
        std::string out = j.value("name", "");
        if (j.contains("arguments")) {
            const auto& a = j["arguments"];
            if (a.is_object() && a.size() == 1 && a.begin()->is_string())
                out += " " + a.begin()->get<std::string>();
            else
                out += " " + a.dump();
        }
        return safe_utf8_truncate(out, 400, "...");
    } catch (...) {
        for (char& c : body)
            if (c == '\n' || c == '\r') c = ' ';
        return safe_utf8_truncate(body, 400, "...");
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

json TaskMemoBuilder::BuildRuleLayer(
    const json& prev, const std::vector<GenieChatMessage>& dropped_messages,
    const std::vector<std::string>& dropped_tool_call_content) const
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
    for (size_t i = 0; i < dropped_messages.size(); ++i) {
        const auto& m = dropped_messages[i];
        std::string preview = ReplaceInvalidUtf8(ExtractKeyFacts(m.content, config_.rule_layer_preview_chars));
        if (m.role == "user") {
            AppendCapped(entry["facts_constraints"], "用户提及: " + preview, kMaxItemsPerField);
            size_t todo_pos = m.content.find("TODO");
            if (todo_pos == std::string::npos)
                todo_pos = m.content.find("todo");
            if (todo_pos != std::string::npos)
                AppendCapped(entry["open_questions"], "待办: " + ReplaceInvalidUtf8(safe_utf8_truncate(m.content.substr(todo_pos), 160, "")), kMaxItemsPerField);
        } else if (m.role == "assistant") {
            const std::string conclusion = TaskMemoBuilder::ExtractThinkConclusion(m.content);
            if (!conclusion.empty())
                AppendCapped(entry["facts_constraints"], "思考结论: " + ReplaceInvalidUtf8(conclusion), kMaxItemsPerField);
            last_assistant_content = m.content;
        } else if (m.role == "tool") {
            bool failed = LooksLikeFailure(m.content);
            entry["tool_state"]["tool_result_" + std::to_string(tool_seq++)] =
                (failed ? "失败: " : "成功: ") + preview;
            CapObjectSize(entry["tool_state"], kMaxItemsPerField);
            std::string call_content;
            if (i < dropped_tool_call_content.size())
                call_content = dropped_tool_call_content[i];
            if (call_content.empty())
                call_content = last_assistant_content;
            const std::string call = ExtractToolCallSummary(call_content);
            AppendCapped(entry["completed"], (failed ? "失败: " : "成功: ") +
                (call.empty() ? std::string() : ReplaceInvalidUtf8(safe_utf8_truncate(call, 200, "...")) + " -> ") + preview, kMaxItemsPerField);
            if (!failed && !call.empty()) {
                const std::string tool = call.substr(0, call.find(' '));
                if (!entry.contains("last_success") || !entry["last_success"].is_object())
                    entry["last_success"] = json::object();
                entry["last_success"][tool] = ReplaceInvalidUtf8(safe_utf8_truncate(FullToolCall(call_content), kMaxLastSuccessChars, "..."));
                CapObjectSize(entry["last_success"], 6);
            }
            const std::string tool_name = call.substr(0, call.find(' '));
            const bool reads_content = IsReadLikeToolName(tool_name) || tool_name.find("fetch") != std::string::npos;
            if (!failed && !call.empty() && !reads_content)
                for (const auto& a : ExtractArtifactLines(m.content))
                    AppendCapped(entry["artifacts"], ReplaceInvalidUtf8(a), kMaxArtifacts);

            // 已尝试查询索引：对同一资源（如同一文件路径）用不同参数反复调用非 read 类工具
            // 时（如 grep 的 pattern 每次不同），现有去重机制（CanonicalToolCallKey，
            // message_pre_filter.cpp）因参数不同判定为全新调用、不会打重复标；这里不做
            // "内容是否已被覆盖"的推断（该路已判死，69% 误判率，见 message_pre_filter.cpp.notes.md），
            // 只如实记录模型自己发出过的调用，交给模型自己判断要不要换个角度，不代为丢弃
            // 或合并任何内容——这与"去重/占位"是两条独立的路，互不替代。
            if (!call.empty() && !reads_content) {
                const std::string query_path = ExtractAnyToolPathArg(call_content);
                if (!query_path.empty()) {
                    if (!entry.contains("tried_queries") || !entry["tried_queries"].is_object())
                        entry["tried_queries"] = json::object();
                    json& bucket = entry["tried_queries"][query_path];
                    const std::string item = (failed ? "失败: " : "") +
                        ReplaceInvalidUtf8(safe_utf8_truncate(call, kMaxHardRuleChars, "..."));
                    AppendCapped(bucket, item, kMaxTriedQueriesPerResource);
                    CapObjectSize(entry["tried_queries"], kMaxTriedQueryResources);
                    // 总尝试次数单独计数、不随 FIFO 截断而丢失：即便早期的具体 pattern 文本已被
                    // 淘汰出数组，模型仍能看到"这个资源已经被查询过多少次"这个常量大小的信号,
                    // 不需要为此保留全部历史文本,也不会破坏非驱逐轮的字节稳定性。
                    if (!entry.contains("tried_queries_count") || !entry["tried_queries_count"].is_object())
                        entry["tried_queries_count"] = json::object();
                    size_t prev_count = entry["tried_queries_count"].value(query_path, (size_t) 0);
                    entry["tried_queries_count"][query_path] = prev_count + 1;
                    CapObjectSize(entry["tried_queries_count"], kMaxTriedQueryResources);
                }
            }

            // 已读文件索引：被驱逐的 read 类工具结果不再丢弃全部痕迹，而是在 Task Memo 里
            // 留一条「路径 + 关键事实摘要」，供模型在原始内容已不在上下文时判断是否要重读。
            {
                std::string read_path = ExtractReadToolPathImpl(call_content);
                if (!read_path.empty()) {
                    entry["read_files"][read_path] = preview;
                    CapObjectSize(entry["read_files"], kMaxItemsPerField);
                    if (!entry.contains("hard_rules") || !entry["hard_rules"].is_array())
                        entry["hard_rules"] = json::array();
                    if (!entry.contains("_hard_rule_files") || !entry["_hard_rule_files"].is_object())
                        entry["_hard_rule_files"] = json::object();
                    if (!entry["_hard_rule_files"].contains(read_path)) {
                        entry["_hard_rule_files"][read_path] = true;
                        for (const auto& rule : ExtractHardRules(m.content)) {
                            if (entry["hard_rules"].size() >= kMaxHardRules) break;
                            const std::string item = ReplaceInvalidUtf8(rule);
                            bool dup = false;
                            for (const auto& r : entry["hard_rules"])
                                if (r.is_string() && r.get<std::string>() == item) { dup = true; break; }
                            if (!dup) entry["hard_rules"].push_back(item);
                        }
                    }
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
    const std::vector<GenieChatMessage> new_dropped(dropped_messages.begin() + (long long) already_folded,
                                                    dropped_messages.end());

    // 连续失败/重复调用三个活体信号提前查询：它们各自独立维护在 ToolCallCircuitBreakerStore/
    // ToolCallRepetitionStore 里，不依赖 dropped_messages 是否有新增，必须在节流判断之前就取到
    // 最新值——否则节流一旦命中，这三个信号会一直复用 *prev 里的陈旧值，在驱逐边界冻结（sticky）
    // 期间永远传不到模型那里，而这恰恰是最需要这些警告生效的时间窗口。
    std::string cb_key = ToolCallCircuitBreakerStore::MakeKey(first_msg_key, instance_config_.get_model_name());
    int failure_streak_live = ToolCallCircuitBreakerStore::GetInstance().GetConsecutiveCount(cb_key);
    const auto &redundant_cfg = instance_config_.i_model_config_.GetToolCallRepairConfig().redundant_call_guard;
    int redundant_tool_call_count_live = 0;
    int non_contiguous_repeat_count_live = 0;
    if (redundant_cfg.enabled) {
        std::string rc_key = ToolCallRepetitionStore::MakeKey(first_msg_key, instance_config_.get_model_name());
        redundant_tool_call_count_live = ToolCallRepetitionStore::GetInstance().GetRepeatCount(rc_key);
        non_contiguous_repeat_count_live = ToolCallRepetitionStore::GetInstance().GetNonContiguousRepeatCount(rc_key);
    }
    bool warning_active =
        (config_.failure_streak_warn_threshold > 0 && failure_streak_live >= config_.failure_streak_warn_threshold) ||
        (redundant_cfg.enabled && redundant_cfg.repeat_threshold > 0
            && redundant_tool_call_count_live >= redundant_cfg.repeat_threshold) ||
        (redundant_cfg.enabled && redundant_cfg.non_contiguous_repeat_threshold > 0
            && non_contiguous_repeat_count_live >= redundant_cfg.non_contiguous_repeat_threshold);

    // 节流：驱逐仍按需每轮发生，但只有自上一次真正重渲染以来新增的丢弃量达到
    // config_.render_throttle_min_new_dropped 才真正重渲染；未达到（含恰好 0 的
    // "无新增"情形）直接复用上一份渲染结果——新增内容仍留在 dropped_messages 里，
    // 不写回 store（_folded_count 不前移），下一次真正触发时通过差分一次性合并，不会丢失。
    // 示警只在"刚变为活跃"这一轮绕开节流（上升沿），活跃期间不再逐轮绕开：持续绕开会让 memo
    // 每轮在原位改写，把前缀复用钉死在 memo 之前（实测复用率 0.81→0.40）。
    const bool prev_warning_latched = has_prev && prev->value("warning_latched", false);
    const bool warning_rising = warning_active && !prev_warning_latched;
    size_t render_throttle = std::max<size_t>(1, config_.render_throttle_min_new_dropped);
    if (has_prev && new_dropped.size() < render_throttle && !warning_rising) {
        res.active = true;
        res.confidence = prev_confidence;
        res.refresh_count = prev_refresh;
        res.pages_total = (prev->contains("pages") && (*prev)["pages"].is_array()) ? (*prev)["pages"].size() : 0;
        res.compact_render = RenderCompact(*prev);
        return res;
    }

    const std::vector<std::string> all_dropped_tool_call_content =
        MatchDroppedToolCallContent(raw_messages, dropped_messages);
    const std::vector<std::string> new_dropped_tool_call_content(
        all_dropped_tool_call_content.begin() + (long long) already_folded,
        all_dropped_tool_call_content.end());
    json entry = BuildRuleLayer(has_prev ? *prev : json::object(), new_dropped,
                                new_dropped_tool_call_content);

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
            for (const char* k : {"hard_rules", "_hard_rule_files", "artifacts", "last_success", "tried_queries", "tried_queries_count"})
                if (entry.contains(k))
                    model_entry[k] = entry[k];
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

    // 连续失败/重复调用三个活体信号已在节流判断之前查询过（见上方 warning_active），这里
    // 直接复用同一份值落盘，不重复查询 store——保持"查一次、决策和落盘用同一份数据"。
    entry["failure_streak"] = failure_streak_live;
    // Render() 是 static 方法读不到 config_，阈值随 entry 一起落盘，保持 Render() 签名
    // 不变（纯函数于 entry），也让每条已存储的备忘录自描述当时生效的阈值是多少。
    entry["failure_streak_warn_threshold"] = config_.failure_streak_warn_threshold;
    entry["memo_reserve_ratio"] = config_.memo_reserve_ratio;

    if (redundant_cfg.enabled) {
        entry["redundant_tool_call_count"] = redundant_tool_call_count_live;
        entry["redundant_tool_call_threshold"] = redundant_cfg.repeat_threshold;
        entry["non_contiguous_repeat_count"] = non_contiguous_repeat_count_live;
        entry["non_contiguous_repeat_threshold"] = redundant_cfg.non_contiguous_repeat_threshold;
    } else {
        entry["redundant_tool_call_count"] = 0;
        entry["redundant_tool_call_threshold"] = 0;
        entry["non_contiguous_repeat_count"] = 0;
        entry["non_contiguous_repeat_threshold"] = 0;
    }

    entry["warning_latched"] = warning_active;
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

    int non_contiguous_repeat_count = entry.value("non_contiguous_repeat_count", 0);
    int non_contiguous_repeat_threshold = entry.value("non_contiguous_repeat_threshold", 0);
    if (non_contiguous_repeat_threshold > 0 && non_contiguous_repeat_count >= non_contiguous_repeat_threshold) {
        goal_block += TaskMemoBuilder::RenderNonContiguousRepeatNote(non_contiguous_repeat_count);
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
        oss << "Files already read whose content has left the context (summary only; re-read a "
               "file only if the summary is missing a detail you actually need now):\n";
        for (auto it = entry["read_files"].begin(); it != entry["read_files"].end(); ++it) {
            oss << "- " << it.key() << ": "
                << (it.value().is_string() ? it.value().get<std::string>() : it.value().dump()) << "\n";
        }
        read_files_block = oss.str();
    }

    // 已尝试查询索引：对同一资源（如同一文件路径）用不同参数反复调用非 read 类工具时
    // （如 grep 的 pattern 每次不同），现有去重机制因参数不同不会判重；这里只如实呈现
    // 模型自己发出过的调用，不做任何"内容是否已被覆盖"的推断，交给模型自己判断是否
    // 要换个角度，而不是代为丢弃或合并。总尝试次数单独计数、不随 FIFO 截断而丢失，
    // 即便早期的具体查询文本已被淘汰，模型依然能看到这个资源被反复查询过多少次。
    std::string tried_queries_block;
    if (entry.contains("tried_queries") && entry["tried_queries"].is_object() && !entry["tried_queries"].empty()) {
        std::ostringstream oss;
        oss << "Queries already tried per resource, drawn from earlier context that has since left "
               "the window (check the most recent messages below in case one of these was actually "
               "resolved since; if your next call targets the same resource with similar intent, "
               "prefer a different angle instead of repeating):\n";
        for (auto it = entry["tried_queries"].begin(); it != entry["tried_queries"].end(); ++it) {
            if (!it.value().is_array() || it.value().empty())
                continue;
            const size_t total = (entry.contains("tried_queries_count") && entry["tried_queries_count"].is_object())
                ? entry["tried_queries_count"].value(it.key(), (size_t) it.value().size())
                : it.value().size();
            oss << "- " << it.key() << " (" << total << " attempt(s) recorded so far";
            if (total > it.value().size())
                oss << ", only the most recent " << it.value().size() << " shown";
            oss << "):\n";
            for (const auto& q : it.value())
                if (q.is_string())
                    oss << "  - " << q.get<std::string>() << "\n";
        }
        tried_queries_block = oss.str();
    }

    std::string last_success_block;
    if (entry.contains("last_success") && entry["last_success"].is_object()
            && !entry["last_success"].empty()) {
        std::ostringstream oss;
        oss << "Last successful call per tool (reuse instead of re-deriving the syntax):\n";
        for (auto it = entry["last_success"].begin(); it != entry["last_success"].end(); ++it)
            if (it.value().is_string())
                oss << "- " << it.value().get<std::string>() << "\n";
        last_success_block = oss.str();
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

    // 由高到低优先级排列；预算放不下时整块跳过。
    const std::string blocks[] = {
        render_list("Rules from documents you read (still in force)", "hard_rules"),
        render_list("Artifacts already produced, confirmed by tool output from earlier context that has "
                    "since left the window (not an exhaustive list of everything done so far; check the "
                    "most recent messages below for newer results before redoing any of these steps)", "artifacts"),
        last_success_block,
        render_list("Completed", "completed"),
        render_list("Next actions", "next_actions"),
        render_list("Facts / constraints", "facts_constraints"),
        render_list("Open questions", "open_questions"),
        read_files_block,
        tried_queries_block,
        tool_state_block,
        page_directory_block,
    };
    static const char* kBlockNames[] = {
        "hard_rules", "artifacts", "last_success", "completed", "next_actions",
        "facts_constraints", "open_questions", "read_files", "tried_queries", "tool_state", "page_directory",
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

    double reserve_ratio = entry.value("memo_reserve_ratio", 0.0);
    size_t budget_for_high_priority = max_chars;
    if (max_chars > 0 && reserve_ratio > 0.0) {
        const size_t reserve_want = read_files_block.size() + tried_queries_block.size() + tool_state_block.size();
        const size_t reserve_cap = static_cast<size_t>(static_cast<double>(max_chars) * reserve_ratio);
        const size_t reserved_for_memory = std::min(reserve_want, reserve_cap);
        budget_for_high_priority = (max_chars > reserved_for_memory) ? (max_chars - reserved_for_memory) : 0;
    }

    size_t block_index = 0;
    for (const auto& block : blocks) {
        const size_t slot = block_index++;
        const char* block_name = kBlockNames[slot];
        if (block.empty())
            continue;
        const bool is_reserved_block = (std::strcmp(block_name, "read_files") == 0
                                         || std::strcmp(block_name, "tried_queries") == 0
                                         || std::strcmp(block_name, "tool_state") == 0);
        const size_t slot_budget = is_reserved_block ? max_chars : budget_for_high_priority;
        if (max_chars > 0 && out.size() + block.size() > slot_budget) {
            My_Log{My_Log::Level::kWarning}
                << "[TaskMemo] Render block skipped: name=" << block_name
                << ", block_chars=" << block.size()
                << ", used_chars=" << out.size()
                << ", max_chars=" << max_chars
                << ", slot_budget=" << slot_budget << std::endl;
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
        << " time(s) in a row. Its result is already in the conversation (or summarized in this memo); "
           "repeating it again is unlikely to give new information. Use that result or try a different approach.\n";
    return oss.str();
}

std::string TaskMemoBuilder::RenderNonContiguousRepeatNote(int repeat_count)
{
    std::ostringstream oss;
    oss << "Note: you have called the same tool with the same arguments " << repeat_count
        << " time(s) so far in this task (not necessarily in a row). Its result is already in the "
           "conversation (or summarized in this memo); repeating it again is unlikely to give new "
           "information. Use that result or try a different approach.\n";
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
