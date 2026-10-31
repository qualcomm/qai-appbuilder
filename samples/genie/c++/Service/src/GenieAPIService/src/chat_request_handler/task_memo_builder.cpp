//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "task_memo_builder.h"
#include "tool_call_circuit_breaker_store.h"
#include "task_memo_doc_scan.h"
#include <utils.h>
#include "log.h"
#include <sstream>
#include <iomanip>
#include <algorithm>
#include <functional>
#include <cstring>
#include <cstdio>
#include <cstdlib>
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

    const uint64_t scope = ComputeScopeHash(messages);
    if (scope != 0) {
        h ^= scope;
        h *= p;
    }

    std::ostringstream oss;
    oss << "tmfmk_" << std::hex << std::setfill('0') << std::setw(16) << h;
    return oss.str();
}

uint64_t TaskMemoBuilder::ComputeScopeHash(const json& messages, const json* request_data)
{
    uint64_t h = 14695981039346656037ULL;
    const uint64_t p = 1099511628211ULL;
    bool any = false;
    auto mix = [&](const std::string& s, bool skip_digits) {
        for (unsigned char c : s) {
            if (skip_digits && std::isdigit(c))
                continue;
            h ^= c;
            h *= p;
        }
        h ^= 0xFF;
        h *= p;
    };
    if (messages.is_array()) {
        for (const auto& msg : messages) {
            const std::string role = msg.value("role", "");
            if (role != "system" && role != "developer")
                continue;
            any = true;
            mix(role, false);
            if (msg.contains("content"))
                mix(msg["content"].is_string() ? msg["content"].get<std::string>() : msg["content"].dump(), true);
        }
    }
    if (request_data && request_data->is_object() && request_data->contains("user")
            && (*request_data)["user"].is_string() && !(*request_data)["user"].get<std::string>().empty()) {
        any = true;
        mix("user", false);
        mix((*request_data)["user"].get<std::string>(), false);
    }
    return any ? h : 0;
}

std::optional<json> TaskMemoBuilder::Lookup(const json& raw_messages) const
{
    if (!config_.enabled || !store_)
        return std::nullopt;
    return store_->Lookup(ComputeFingerprint(raw_messages), ComputeFirstMsgKey(raw_messages));
}

namespace {
    constexpr size_t kMaxItemsPerField = 20;
    constexpr size_t kMaxHardRules = 12;
    constexpr size_t kMaxHardRulesPerFile = 6;
    constexpr size_t kMaxHardRuleChars = 220;
    constexpr size_t kMaxArtifacts = 8;
    constexpr size_t kMaxLastSuccessChars = 900;

    constexpr size_t kMaxTriedQueryResources = 10;
    constexpr size_t kMaxTriedQueriesPerResource = 6;
    constexpr size_t kMaxCommandResourceKeyChars = 120;

    constexpr size_t kMaxPendingRefs = 3;
    constexpr size_t kMaxRecoveryEpisodes = 8;
    constexpr size_t kMaxExecCacheEntries = 10;
    constexpr size_t kMaxRecoveryAttempts = 6;

    constexpr size_t kMaxPendingRepeatDetections = 24;

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

    struct WriteCall {
        std::string key;
        std::string summary;
    };

    bool ParseToolCallArgs(const std::string& assistant_content, std::string& name, json& args) {
        const std::string visible = TaskMemoBuilder::StripThinkBlock(assistant_content);
        const size_t tc = visible.find("<tool_call>");
        if (tc == std::string::npos)
            return false;
        const size_t end = visible.find("</tool_call>", tc);
        const json call = json::parse(visible.substr(tc + 11, end == std::string::npos ? std::string::npos : end - tc - 11),
                                      nullptr, false);
        if (!call.is_object())
            return false;
        name = call.value("name", std::string());
        args = call.value("arguments", json::object());
        if (args.is_string())
            args = json::parse(args.get<std::string>(), nullptr, false);
        return args.is_object();
    }

    WriteCall SummarizeWriteCall(const std::string& assistant_content) {
        WriteCall out;
        std::string name;
        json args;
        if (!ParseToolCallArgs(assistant_content, name, args))
            return out;
        std::string lower = name;
        std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char c) { return (char) tolower(c); });
        if (lower.find("write") == std::string::npos && lower.find("edit") == std::string::npos)
            return out;
        std::string path;
        for (const char* k : { "path", "file_path", "file", "filename" }) {
            if (args.contains(k) && args[k].is_string()) {
                path = args[k].get<std::string>();
                break;
            }
        }
        std::string text;
        if (args.contains("content") && args["content"].is_string()) {
            text = args["content"].get<std::string>();
        } else if (args.contains("edits") && args["edits"].is_array()) {
            for (const auto& e : args["edits"])
                if (e.is_object() && e.contains("newText") && e["newText"].is_string())
                    text += e["newText"].get<std::string>() + "\n";
        }
        if (path.empty() || text.empty())
            return out;
        out.key = name + " " + path + " :: ";
        out.summary = out.key + task_memo_scan::ExtractCodeExcerpt(text, 300);
        return out;
    }

    std::string CompactExecCall(const std::string& assistant_content) {
        std::string name;
        json args;
        if (!ParseToolCallArgs(assistant_content, name, args))
            return "";
        for (const char* k : { "command", "cmd", "script" }) {
            if (args.contains(k) && args[k].is_string()) {
                const std::string compact = task_memo_scan::CompactCommand(args[k].get<std::string>());
                return compact.empty() ? compact : name + " " + compact;
            }
        }
        return "";
    }

    std::string StripToolWrapper(const std::string& content) {
        std::string out;
        for (const std::string& line : task_memo_scan::SplitLines(content)) {
            const std::string t = task_memo_scan::Trim(line);
            if (t == "<tool_response>" || t == "</tool_response>" || t.rfind("[snapshot:", 0) == 0)
                continue;
            if (!out.empty())
                out += '\n';
            out += line;
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
            "Failed to", "failed to", "Failed:", "error:", "FAILED", "failed with",
            "Cannot find", "cannot find",
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

    bool HasNonZeroExitMarker(const std::string& content) {
        const size_t pos = content.rfind("[exit code:");
        return pos != std::string::npos && std::strtol(content.c_str() + pos + 11, nullptr, 10) != 0;
    }

    bool LooksLikeFailure(const std::string& content) {
        if (task_memo_scan::HasPagedReadFooter(content))
            return false;
        if (HasNonZeroExitMarker(content))
            return true;
        size_t pos = 0;
        while (pos <= content.size()) {
            const size_t nl = content.find('\n', pos);
            const size_t end = (nl == std::string::npos) ? content.size() : nl;
            const std::string line = content.substr(pos, end - pos);
            if (!task_memo_scan::IsRuntimeLogLine(line) && LineHasFailureSignal(line))
                return true;
            if (nl == std::string::npos)
                break;
            pos = nl + 1;
        }
        if (HasNonEmptyErrorField(content, "\"error\"") || HasNonEmptyErrorField(content, "\"is_error\""))
            return true;
        return false;
    }

    bool IsWrapperLine(const std::string& trimmed) {
        static const char* kPrefixes[] = {
            "[exit code", "---", "[full_output_saved", "[truncation_advice", "[stderr]", "DSP_INFO",
            "<tool_response>", "</tool_response>",
        };
        for (const char* p : kPrefixes)
            if (trimmed.rfind(p, 0) == 0)
                return true;
        return false;
    }

    bool IsPythonExceptionLine(const std::string& line) {
        if (line.empty() || std::isspace(static_cast<unsigned char>(line[0])))
            return false;
        size_t end = 0;
        while (end < line.size() && (std::isalnum(static_cast<unsigned char>(line[end])) || line[end] == '_' || line[end] == '.'))
            ++end;
        if (end == 0 || (end < line.size() && line[end] != ':' && line[end] != '\r'))
            return false;
        const std::string ident = line.substr(0, end);
        for (const char* suffix : { "Error", "Exception" }) {
            const size_t n = strlen(suffix);
            if (ident.size() > n && ident.compare(ident.size() - n, n, suffix) == 0)
                return true;
        }
        return ident == "KeyboardInterrupt" || ident == "SystemExit";
    }

    std::string LastPythonExceptionLine(const std::string& content) {
        std::string found;
        size_t pos = 0;
        while (pos <= content.size()) {
            const size_t nl = content.find('\n', pos);
            const size_t end = (nl == std::string::npos) ? content.size() : nl;
            const std::string line = content.substr(pos, end - pos);
            if (IsPythonExceptionLine(line))
                found = task_memo_scan::Trim(line);
            if (nl == std::string::npos)
                break;
            pos = nl + 1;
        }
        return found;
    }

    std::string LastOutputLine(const std::string& content) {
        std::string last;
        size_t pos = 0;
        while (pos <= content.size()) {
            const size_t nl = content.find('\n', pos);
            const size_t end = (nl == std::string::npos) ? content.size() : nl;
            const std::string trimmed = task_memo_scan::Trim(content.substr(pos, end - pos));
            if (!trimmed.empty() && !IsWrapperLine(trimmed))
                last = trimmed;
            if (nl == std::string::npos)
                break;
            pos = nl + 1;
        }
        return last;
    }

    std::string FirstFailureLineImpl(const std::string& content) {
        if (!LooksLikeFailure(content))
            return "";
        const std::string exception = LastPythonExceptionLine(content);
        if (!exception.empty())
            return safe_utf8_truncate(exception, 110, "");
        std::string fallback;
        size_t pos = 0;
        while (pos <= content.size()) {
            const size_t nl = content.find('\n', pos);
            const size_t end = (nl == std::string::npos) ? content.size() : nl;
            const std::string line = content.substr(pos, end - pos);
            if (LineHasFailureSignal(line)) {
                if (line.find("Traceback (most recent call last)") == std::string::npos)
                    return task_memo_scan::Trim(line);
                if (fallback.empty())
                    fallback = "Traceback";
            }
            if (nl == std::string::npos)
                break;
            pos = nl + 1;
        }
        if (!fallback.empty())
            return fallback;
        if (!HasNonZeroExitMarker(content))
            return "structured_error";
        const std::string tail = LastOutputLine(content);
        return tail.empty() ? std::string("nonzero_exit") : "exit: " + safe_utf8_truncate(tail, 110, "");
    }

    bool IsStatusLine(const std::string& trimmed) {
        if (trimmed.size() < 3 || trimmed.size() > 48 || !std::isalpha(static_cast<unsigned char>(trimmed[0])))
            return false;
        size_t plain = 0;
        for (char c : trimmed)
            if (std::isalpha(static_cast<unsigned char>(c)) || c == ' ' || c == '.' || c == '-' || c == '_')
                ++plain;
        return plain * 10 >= trimmed.size() * 7 && !LineHasFailureSignal(trimmed);
    }

    std::string ExtractFailureFacts(const std::string& content, size_t max_len) {
        const std::vector<std::string> lines = task_memo_scan::SplitLines(content);
        const std::string exception = LastPythonExceptionLine(content);
        const size_t signal_limit = exception.empty() ? 2 : 1;
        const size_t signal_chars = exception.empty() ? 80 : 60;
        size_t head = std::string::npos;
        size_t first_signal = std::string::npos;
        size_t exception_line = std::string::npos;
        std::vector<std::string> signals;
        for (size_t i = 0; i < lines.size(); ++i) {
            const std::string trimmed = task_memo_scan::Trim(lines[i]);
            if (trimmed.empty() || IsWrapperLine(trimmed))
                continue;
            if (head == std::string::npos)
                head = i;
            if (IsPythonExceptionLine(lines[i]))
                exception_line = i;
            if (LineHasFailureSignal(lines[i]) && trimmed.find("Traceback (most recent call last)") == std::string::npos) {
                if (first_signal == std::string::npos)
                    first_signal = i;
                if (signals.size() < signal_limit)
                    signals.push_back(safe_utf8_truncate(trimmed, signal_chars, "..."));
            }
        }
        std::vector<std::string> parts;
        auto add = [&parts](const std::string& s) {
            if (!s.empty() && std::find(parts.begin(), parts.end(), s) == parts.end())
                parts.push_back(s);
        };
        if (head != std::string::npos)
            add(safe_utf8_truncate(task_memo_scan::Trim(lines[head]), 50, "..."));
        const size_t anchor = std::min(first_signal, exception_line);
        if (anchor != std::string::npos && head != std::string::npos) {
            for (size_t i = anchor; i-- > head + 1;) {
                const std::string trimmed = task_memo_scan::Trim(lines[i]);
                if (IsStatusLine(trimmed)) {
                    add(trimmed);
                    break;
                }
            }
        }
        for (const auto& s : signals)
            add(s);
        add(safe_utf8_truncate(exception, 90, "..."));
        if (HasNonZeroExitMarker(content)) {
            const size_t pos = content.rfind("[exit code:");
            const size_t close = content.find(']', pos);
            if (close != std::string::npos && close - pos < 24)
                add(content.substr(pos, close - pos + 1));
        }
        std::string out;
        for (const auto& p : parts)
            out += (out.empty() ? "" : " | ") + p;
        return safe_utf8_truncate(out, max_len, "...");
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

        return lower.find("read") != std::string::npos && lower.find("exec") == std::string::npos;
    }

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

    std::string DecodeJsonStringBody(const std::string& raw) {
        if (raw.find('\\') == std::string::npos)
            return raw;
        const json decoded = json::parse("\"" + raw + "\"", nullptr, false);
        return decoded.is_string() ? decoded.get<std::string>() : raw;
    }

    std::string ExtractPathArgFrom(const std::string& assistant_content, size_t search_from) {
        static const char* kPathKeys[] = { "\"path\"", "\"file\"", "\"file_path\"", "\"filename\"", "\"output\"", "\"dest\"" };
        for (const char* key : kPathKeys) {
            size_t kp = assistant_content.find(key, search_from);
            if (kp == std::string::npos)
                continue;
            size_t colon = assistant_content.find(':', kp);
            size_t q1 = (colon == std::string::npos) ? std::string::npos : assistant_content.find('"', colon + 1);
            size_t q2 = (q1 == std::string::npos) ? std::string::npos : assistant_content.find('"', q1 + 1);
            if (q1 != std::string::npos && q2 != std::string::npos)
                return DecodeJsonStringBody(assistant_content.substr(q1 + 1, q2 - q1 - 1));
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

    std::string ExtractIdempotentExecCommandImpl(const std::string& assistant_content) {
        const std::string visible = TaskMemoBuilder::StripThinkBlock(assistant_content);
        std::string name;
        size_t search_from = 0;
        if (!ExtractToolNameAndSearchFrom(visible, name, search_from))
            return "";
        std::string lower_name = name;
        std::transform(lower_name.begin(), lower_name.end(), lower_name.begin(),
                       [](unsigned char c) { return (char) tolower(c); });
        if (lower_name != "exec")
            return "";
        const std::string command = DecodeJsonStringBody(ExtractCommandArgFrom(visible, search_from));

        const std::string normalized = task_memo_scan::NormalizeExecCommandForCache(command);
        if (!task_memo_scan::IsIdempotentExecCommand(normalized))
            return "";
        return normalized;
    }

    using task_memo_scan::LineRange;
    using task_memo_scan::LoadLineRanges;
    using task_memo_scan::JoinLineRanges;

    std::string StripRangeSuffix(const std::string& read_path) {
        return read_path.substr(0, read_path.find("#range:"));
    }

    bool ComputeReadRange(const std::string& read_path, const std::string& result,
                          size_t& first, size_t& last, size_t& total) {
        const size_t marker = result.rfind("showed lines ");
        if (marker != std::string::npos &&
            std::sscanf(result.c_str() + marker, "showed lines %zu-%zu of total %zu", &first, &last, &total) == 3)
            return true;
        first = 1;
        total = 0;
        const size_t offset_pos = read_path.find("offset=");
        if (offset_pos != std::string::npos)
            first = std::max<size_t>(1, static_cast<size_t>(std::strtoull(read_path.c_str() + offset_pos + 7, nullptr, 10)));
        size_t lines = 0;
        size_t pos = 0;
        while (pos < result.size()) {
            if (result.compare(pos, 10, "[snapshot:") != 0)
                ++lines;
            const size_t nl = result.find('\n', pos);
            if (nl == std::string::npos)
                break;
            pos = nl + 1;
        }
        if (lines == 0)
            return false;
        last = first + lines - 1;
        return true;
    }

    std::string RenderReadFilesBlock(const json& read_files, size_t cap) {
        const std::string header =
            "Files already read, as line ranges whose text has left the context (state as of the last compaction):\n";
        std::vector<std::string> keys;
        std::vector<const json*> values;
        for (auto it = read_files.begin(); it != read_files.end(); ++it) {
            keys.push_back(it.key());
            values.push_back(&it.value());
        }
        const size_t n = keys.size();
        std::vector<std::string> bases(n);
        for (size_t i = 0; i < n; ++i) {
            std::string name = task_memo_scan::DisplayPathName(keys[i], false);
            for (size_t j = 0; j < n; ++j) {
                if (j != i && task_memo_scan::DisplayPathName(keys[j], false) == name) {
                    name = task_memo_scan::DisplayPathName(keys[i], true);
                    break;
                }
            }
            bases[i] = task_memo_scan::RenderReadFileBase(name, *values[i]);
        }
        auto omitted_note = [](size_t dropped) {
            return dropped == 0 ? std::string()
                                : "- (" + std::to_string(dropped) + " earlier file(s) omitted to fit)\n";
        };
        std::vector<size_t> tail(n + 1, 0);
        for (size_t i = n; i-- > 0;)
            tail[i] = tail[i + 1] + bases[i].size() + 1;
        size_t first = 0;
        while (first + 1 < n && header.size() + omitted_note(first).size() + tail[first] > cap)
            ++first;
        const size_t fixed = header.size() + omitted_note(first).size() + tail[first];
        size_t remaining = cap > fixed ? cap - fixed : 0;
        std::vector<std::string> suffixes(n);
        for (size_t i = n; i-- > first;) {
            const size_t share = std::min(task_memo_scan::kReadFileSuffixChars, remaining / (i - first + 1));
            suffixes[i] = task_memo_scan::RenderReadFileSuffix(*values[i], share);
            remaining -= suffixes[i].size();
        }
        std::string out = header + omitted_note(first);
        for (size_t i = first; i < n; ++i)
            out += bases[i] + suffixes[i] + "\n";
        return out;
    }

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
        if (LooksLikeFailure(content))
            return ExtractFailureFacts(content, max_len);

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
            const std::string cleaned = StripToolWrapper(content);
            if (cleaned.size() <= std::min<size_t>(120, max_len))
                return cleaned;
            static const char* kPathHints[] = { "path\":", "file\":", "C:\\", "/home/", "/root/", "/mnt/", "/usr/" };
            for (const char* hint : kPathHints) {
                const size_t pos = cleaned.find(hint);
                if (pos == std::string::npos)
                    continue;
                const size_t line_begin = cleaned.rfind('\n', pos);
                const size_t line_end = cleaned.find('\n', pos);
                const size_t begin = line_begin == std::string::npos ? 0 : line_begin + 1;
                const std::string line = task_memo_scan::Trim(
                    cleaned.substr(begin, line_end == std::string::npos ? std::string::npos : line_end - begin));
                return task_memo_scan::ClipKeepTail(line, max_len);
            }
            return safe_utf8_truncate(cleaned, max_len, "...");
        }

        std::string out;
        for (const auto& f : facts) {
            if (!out.empty()) out += "; ";
            out += f;
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

bool TaskMemoBuilder::IsReasoningNoteMimicry(const std::string& content)
{
    return content.rfind(kPinPrefix, 0) == 0;
}

std::string TaskMemoBuilder::ExtractThinkConclusion(const std::string& content)
{
    if (IsReasoningNoteMimicry(content)) {
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

std::string TaskMemoBuilder::FirstFailureLine(const std::string& content)
{
    return FirstFailureLineImpl(content);
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
    if (!entry.contains("pending_refs") || !entry["pending_refs"].is_object())
        entry["pending_refs"] = json::object();
    if (!entry.contains("_episode_attempts") || !entry["_episode_attempts"].is_array())
        entry["_episode_attempts"] = json::array();

    size_t tool_seq = entry["tool_state"].size();
    bool episode_active = entry.value("_episode_active", false);
    size_t episode_attempt_count = entry.value("_episode_attempt_count", (size_t) 0);

    const auto &redundant_cfg = instance_config_.i_model_config_.GetToolCallRepairConfig().redundant_call_guard;
    std::string last_assistant_content;
    auto close_episode = [&](bool resolved, const std::string& detail) {
        if (!episode_active)
            return;
        std::ostringstream row;
        if (resolved) {
            row << "已解决 (第 " << episode_attempt_count << " 次尝试): " << detail;
        } else {
            row << "未解决：同类尝试 " << episode_attempt_count << " 次，全部失败";
            bool first = true;
            for (const auto& attempt : entry["_episode_attempts"])
                if (attempt.is_string()) {
                    row << (first ? "：" : " | ") << attempt.get<std::string>();
                    first = false;
                }
        }
        task_memo_scan::AppendRecoveryEpisode(entry["recovery_ledger"], row.str(), kMaxRecoveryEpisodes);
        episode_active = false;
        episode_attempt_count = 0;
        entry["_episode_attempts"] = json::array();
    };
    for (size_t i = 0; i < dropped_messages.size(); ++i) {
        const auto& m = dropped_messages[i];
        const bool binary = m.role == "tool" &&
            (task_memo_scan::IsBinaryContent(m.content) || m.content.rfind("[binary content, ", 0) == 0);
        const bool tool_failed = m.role == "tool" && LooksLikeFailure(m.content);
        std::string preview;
        if (binary)
            preview = task_memo_scan::BinaryResultStub(m.content.size());
        else if (m.role == "tool" && !tool_failed)
        {
            json parsed = json::parse(m.content, nullptr, false);
            preview = ReplaceInvalidUtf8(parsed.is_discarded()
                ? task_memo_scan::TailFacts(m.content, config_.rule_layer_preview_chars)
                : ExtractKeyFacts(m.content, config_.rule_layer_preview_chars));
        }
        else
            preview = ReplaceInvalidUtf8(ExtractKeyFacts(
                m.content, tool_failed ? config_.rule_layer_preview_chars * 2 : config_.rule_layer_preview_chars));
        if (m.role == "user") {
            close_episode(false, "");
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
            const bool failed = tool_failed;
            std::string call_content = i < dropped_tool_call_content.size() ? dropped_tool_call_content[i] : std::string();
            if (call_content.empty())
                call_content = last_assistant_content;
            const std::string read_path = ExtractReadToolPathImpl(call_content);
            size_t shown_first = 0, shown_last = 0, shown_total = 0;
            const bool indexed_read = !read_path.empty() &&
                ComputeReadRange(read_path, m.content, shown_first, shown_last, shown_total) && !failed && !binary;
            const bool suppress_binary_read = binary && !read_path.empty();
            if (!indexed_read && !suppress_binary_read) {
                entry["tool_state"]["tool_result_" + std::to_string(tool_seq++)] =
                    (failed ? "失败: " : "成功: ") + preview;
                CapObjectSize(entry["tool_state"], kMaxItemsPerField);
            }
            const std::string call = ExtractToolCallSummary(call_content);
            const WriteCall write_call = failed ? WriteCall{} : SummarizeWriteCall(call_content);
            const std::string compact_call = write_call.summary.empty() ? CompactExecCall(call_content) : std::string();
            const std::string call_text = ReplaceInvalidUtf8(
                !write_call.summary.empty() ? write_call.summary
                : !compact_call.empty()     ? compact_call
                                            : safe_utf8_truncate(call, 200, "..."));
            if (failed) {
                episode_active = true;
                ++episode_attempt_count;
                const std::string failure_line = FirstFailureLineImpl(m.content);
                AppendCapped(entry["_episode_attempts"], call_text + " -> " +
                    (failure_line.empty() ? preview : failure_line), kMaxRecoveryAttempts);
            } else if (episode_active) {
                close_episode(true, call_text + " -> " + preview);
            }
            const std::string tool_name = call.substr(0, call.find(' '));
            const bool reads_content = IsReadLikeToolName(tool_name) || tool_name.find("fetch") != std::string::npos;
            std::string action_source;
            if (!indexed_read && !reads_content && !call.empty()) {
                const std::vector<std::string> call_tokens = task_memo_scan::SplitNameTokens(FullToolCall(call_content));
                for (auto it = entry["read_files"].begin(); it != entry["read_files"].end() && action_source.empty(); ++it) {
                    if (!it.value().is_object() || !it.value().contains("idents"))
                        continue;
                    const int source_line = task_memo_scan::FindIdentLine(it.value()["idents"], call_tokens);
                    if (source_line > 0) {
                        it.value()["used_line"] = source_line;
                        action_source = " | likely from " + task_memo_scan::LastNameOf(it.key()) + " L" + std::to_string(source_line);
                    }
                }
            }
            if (!indexed_read && !suppress_binary_read)
                task_memo_scan::AppendCompletedRow(entry["completed"], (failed ? "失败: " : "成功: ") +
                    (call.empty() ? std::string() : call_text + " -> ") + preview + action_source, kMaxItemsPerField,
                    !write_call.key.empty() ? write_call.key : compact_call);
            if (!failed && !binary && !call.empty()) {
                const std::string tool = call.substr(0, call.find(' '));
                if (!entry.contains("last_success") || !entry["last_success"].is_object())
                    entry["last_success"] = json::object();
                entry["last_success"][tool] = ReplaceInvalidUtf8(safe_utf8_truncate(FullToolCall(call_content), kMaxLastSuccessChars, "..."));
                CapObjectSize(entry["last_success"], 6);
            }
            if (!failed && !binary) {
                const std::string idempotent_cmd = ExtractIdempotentExecCommandImpl(call_content);
                if (!idempotent_cmd.empty()) {
                    const std::string cache_key = safe_utf8_truncate(idempotent_cmd, kMaxCommandResourceKeyChars, "...");
                    if (!entry.contains("exec_cache") || !entry["exec_cache"].is_object())
                        entry["exec_cache"] = json::object();
                    entry["exec_cache"][cache_key] = ReplaceInvalidUtf8(preview);
                    CapObjectSize(entry["exec_cache"], kMaxExecCacheEntries);
                }
            }

            if (!failed && !call.empty() && !reads_content) {
                std::string claim_path = ExtractAnyToolPathArg(call_content);
                if (claim_path.rfind("cmd:", 0) == 0)
                    claim_path.clear();
                if (!claim_path.empty()) {
                    task_memo_scan::ClaimArtifact(entry["artifacts"], claim_path,
                        ReplaceInvalidUtf8(safe_utf8_truncate(call, kMaxHardRuleChars, "...")));
                    CapObjectSize(entry["artifacts"], kMaxArtifacts);
                }
            }

            if (entry.contains("artifacts") && entry["artifacts"].is_object() && !entry["artifacts"].empty()) {

                const bool probe_is_command_text = read_path.empty();
                std::string confirm_probe = probe_is_command_text ? ExtractIdempotentExecCommandImpl(call_content)
                                                                    : StripRangeSuffix(read_path);
                if (!confirm_probe.empty()) {
                    const std::string hit = task_memo_scan::FindArtifactHit(entry["artifacts"], confirm_probe,
                                                                             probe_is_command_text);
                    if (!hit.empty())
                        task_memo_scan::ConfirmOrRevokeArtifact(entry["artifacts"], hit, !failed);
                }
            }

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

                    if (!entry.contains("tried_queries_count") || !entry["tried_queries_count"].is_object())
                        entry["tried_queries_count"] = json::object();
                    size_t prev_count = entry["tried_queries_count"].value(query_path, (size_t) 0);
                    entry["tried_queries_count"][query_path] = prev_count + 1;
                    CapObjectSize(entry["tried_queries_count"], kMaxTriedQueryResources);
                }
            }

            {
                if (!read_path.empty()) {
                    if (indexed_read) {
                        const std::string read_key = StripRangeSuffix(read_path);

                        constexpr size_t kOverlapRecencyWindow = 48;
                        const size_t current_read_seq = entry.value("_read_call_seq", (size_t) 0) + 1;
                        entry["_read_call_seq"] = current_read_seq;
                        if (redundant_cfg.enabled && entry["read_files"].contains(read_key)) {
                            const size_t offset_pos = read_path.find("#range:offset=");
                            const unsigned long long offset_value = (offset_pos == std::string::npos) ? 0ULL :
                                std::strtoull(read_path.c_str() + offset_pos + strlen("#range:offset="), nullptr, 10);
                            size_t overlap_first = 0, overlap_last = 0;
                            if (offset_value != 0 &&
                                TaskMemoBuilder::DetectRecentOverlapReread(entry["read_files"][read_key], shown_first,
                                                                            shown_last, current_read_seq,
                                                                            kOverlapRecencyWindow, overlap_first,
                                                                            overlap_last)) {
                                entry["overlap_reread_count"] = entry.value("overlap_reread_count", 0) + 1;
                                entry["_overlap_reread_path"] = read_key;
                                entry["_overlap_reread_range"] =
                                    std::to_string(overlap_first) + "-" + std::to_string(overlap_last);
                            }
                        }
                        const task_memo_scan::PageText page = task_memo_scan::ExtractPageText(m.content, shown_first);
                        const std::string seen_before =
                            entry["read_files"].contains(read_key)
                                ? JoinLineRanges(LoadLineRanges(entry["read_files"][read_key].value("seen", json::array())))
                                : std::string();
                        task_memo_scan::AddReadRange(entry["read_files"][read_key], shown_first, shown_last,
                                                     shown_total);

                        task_memo_scan::ApplyOverlapProgressReset(
                            entry, read_key, seen_before,
                            JoinLineRanges(LoadLineRanges(entry["read_files"][read_key].value("seen", json::array()))));

                        entry["read_files"][read_key]["_last_touch_seq"] = current_read_seq;
                        entry["read_files"][read_key]["_touch_count"] =
                            entry["read_files"][read_key].value("_touch_count", (size_t) 0) + 1;
                        task_memo_scan::FoldPage(entry["read_files"][read_key], page.text, page.first);
                        task_memo_scan::ClearReadRef(entry["pending_refs"], read_key);
                        const json pending_before = entry["pending_refs"];
                        task_memo_scan::FoldRefs(entry["pending_refs"], task_memo_scan::ExtractDocRefs(page.text, 4, 160),
                                                 entry["read_files"], read_key, kMaxPendingRefs);
                        for (auto pit = pending_before.begin(); pit != pending_before.end(); ++pit) {
                            if (!entry["pending_refs"].contains(pit.key()))
                                My_Log{My_Log::Level::kInfo} << "[TaskMemo] Pending ref evicted: path=" << pit.key()
                                                             << ", marked=" << task_memo_scan::IsMarkedRef(pit.value())
                                                             << ", while_folding=" << read_key
                                                             << ", pending=" << entry["pending_refs"].size() << "/"
                                                             << kMaxPendingRefs << std::endl;
                        }
                        task_memo_scan::EvictReadFilesByValue(entry["read_files"], kMaxItemsPerField);
                    }
                    if (!entry.contains("hard_rules") || !entry["hard_rules"].is_array())
                        entry["hard_rules"] = json::array();
                    if (!entry.contains("_hard_rule_files") || !entry["_hard_rule_files"].is_object())
                        entry["_hard_rule_files"] = json::object();
                    if (!entry["_hard_rule_files"].contains(read_path)) {
                        entry["_hard_rule_files"][read_path] = true;
                        for (const auto& rule : task_memo_scan::ExtractHardRules(m.content, kMaxHardRulesPerFile, kMaxHardRuleChars)) {
                            task_memo_scan::AdmitHardRule(entry["hard_rules"], entry["_hard_rule_src"],
                                                          ReplaceInvalidUtf8(rule),
                                                          task_memo_scan::NormalizePath(StripRangeSuffix(read_path)), kMaxHardRules);
                        }
                    }
                }
            }
        }
    }

    entry["_episode_active"] = episode_active;
    entry["_episode_attempt_count"] = episode_attempt_count;

    size_t folded_before = entry.value("_folded_count", (size_t)0);
    size_t folded = folded_before + dropped_messages.size();
    entry["_folded_count"] = folded;
    entry["source_range"] = "messages[0:" + std::to_string(folded) + ")";

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

std::pair<std::string, bool> TaskMemoBuilder::ExtractGoalAnchor(const json& raw_messages) const
{
    if (!raw_messages.is_array() || raw_messages.empty())
        return {"", false};

    std::vector<std::string> candidates;
    for (const auto& msg : raw_messages) {
        if (msg.value("role", "") != "user")
            continue;

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

    return {safe_utf8_truncate(candidates[0], config_.goal_anchor_preview_chars, ""), false};
}

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

void TaskMemoBuilder::RecordRepeatDetections(const std::vector<RepeatDetection>& detections)
{
    for (const auto& d : detections) {
        pending_repeat_detections_.push_back(d);
        if (pending_repeat_detections_.size() > kMaxPendingRepeatDetections)
            pending_repeat_detections_.erase(pending_repeat_detections_.begin());
    }
}

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

    size_t render_throttle = std::max<size_t>(1, config_.render_throttle_min_new_dropped);
    if (has_prev && new_dropped.size() < render_throttle) {
        res.active = true;
        res.confidence = prev_confidence;
        res.refresh_count = prev_refresh;
        res.pages_total = (prev->contains("pages") && (*prev)["pages"].is_array()) ? (*prev)["pages"].size() : 0;
        res.compact_render = RenderCompact(*prev);
        return res;
    }

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

    const std::vector<std::string> all_dropped_tool_call_content =
        MatchDroppedToolCallContent(raw_messages, dropped_messages);
    const std::vector<std::string> new_dropped_tool_call_content(
        all_dropped_tool_call_content.begin() + (long long) already_folded,
        all_dropped_tool_call_content.end());
    json entry = BuildRuleLayer(has_prev ? *prev : json::object(), new_dropped,
                                new_dropped_tool_call_content);

    for (const auto& detection : pending_repeat_detections_) {
        const std::string normalized = task_memo_scan::NormalizeExecCommandForCache(detection.command);
        if (!task_memo_scan::IsIdempotentExecCommand(normalized))
            continue;
        const bool binary = task_memo_scan::IsBinaryContent(detection.result_content) ||
            detection.result_content.rfind("[binary content, ", 0) == 0;
        if (binary || LooksLikeFailure(detection.result_content))
            continue;
        json parsed = json::parse(detection.result_content, nullptr, false);
        const std::string preview = ReplaceInvalidUtf8(parsed.is_discarded()
            ? task_memo_scan::TailFacts(detection.result_content, config_.rule_layer_preview_chars)
            : ExtractKeyFacts(detection.result_content, config_.rule_layer_preview_chars));
        const std::string cache_key = safe_utf8_truncate(normalized, kMaxCommandResourceKeyChars, "...");
        if (!entry.contains("exec_cache") || !entry["exec_cache"].is_object())
            entry["exec_cache"] = json::object();
        entry["exec_cache"][cache_key] = preview;
        CapObjectSize(entry["exec_cache"], kMaxExecCacheEntries);
    }
    pending_repeat_detections_.clear();

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

            model_entry["pages"] = entry["pages"];
            model_entry["_next_page_id"] = entry["_next_page_id"];

            model_entry["read_files"] = entry["read_files"];
            for (const char* k : {"hard_rules", "_hard_rule_src", "_hard_rule_files", "pending_refs", "artifacts", "last_success", "exec_cache", "tried_queries", "tried_queries_count", "recovery_ledger", "_episode_active", "_episode_attempt_count", "_episode_attempts"})
                if (entry.contains(k))
                    model_entry[k] = entry[k];

            model_entry["original_goal_raw"] = entry["original_goal_raw"];
            model_entry["original_goal_confidence"] = entry["original_goal_confidence"];

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

    entry["failure_streak"] = failure_streak_live;

    entry["failure_streak_warn_threshold"] = config_.failure_streak_warn_threshold;
    entry["memo_reserve_ratio"] = config_.memo_reserve_ratio;

    if (redundant_cfg.enabled) {
        entry["redundant_tool_call_count"] = redundant_tool_call_count_live;
        entry["redundant_tool_call_threshold"] = redundant_cfg.repeat_threshold;
        entry["non_contiguous_repeat_count"] = non_contiguous_repeat_count_live;
        entry["non_contiguous_repeat_threshold"] = redundant_cfg.non_contiguous_repeat_threshold;

        entry["overlap_reread_threshold"] = redundant_cfg.overlap_reread_threshold;
    } else {
        entry["redundant_tool_call_count"] = 0;
        entry["redundant_tool_call_threshold"] = 0;
        entry["non_contiguous_repeat_count"] = 0;
        entry["non_contiguous_repeat_threshold"] = 0;
        entry["overlap_reread_threshold"] = 0;
    }

    std::string last_read_ref;
    for (auto it = raw_messages.rbegin(); it != raw_messages.rend(); ++it) {
        if (!it->is_object() || it->value("role", "") != "assistant")
            continue;
        last_read_ref = ExtractReadToolPathImpl(get_json_value<std::string>(*it, "content", ""));
        break;
    }
    entry["_last_read_ref"] = last_read_ref;

    entry["refresh_count"] = prev_refresh + 1;
    if (has_prev) {
        const std::string before = Render(*prev, 0);
        const std::string after = Render(entry, 0);
        size_t first_diff = 0;
        const size_t common = std::min(before.size(), after.size());
        while (first_diff < common && before[first_diff] == after[first_diff])
            ++first_diff;
        My_Log{My_Log::Level::kInfo}
            << "[TaskMemo] Rebuilt: refresh_count=" << prev_refresh + 1
            << ", new_dropped=" << new_dropped.size()
            << ", old_chars=" << before.size()
            << ", new_chars=" << after.size()
            << ", first_diff_byte=" << first_diff << " (unbudgeted render)" << std::endl;
    }
    store_->Put(fingerprint, first_msg_key, entry);

    res.active = true;
    res.confidence = entry.value("confidence", 0.0);
    res.refresh_count = entry.value("refresh_count", (size_t)0);
    res.pages_total = (entry.contains("pages") && entry["pages"].is_array()) ? entry["pages"].size() : 0;
    res.compact_render = RenderCompact(entry);
    return res;
}

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

    auto render_list_fit = [&](const char* label, const char* key, size_t budget) -> std::string {
        if (!entry.contains(key) || !entry[key].is_array() || entry[key].empty())
            return "";
        const std::string head = std::string(label) + " (some rows omitted to fit):\n";
        if (head.size() >= budget)
            return "";
        const size_t n = entry[key].size();
        std::vector<char> keep(n, 0);
        size_t used = head.size();
        for (int pass = 0; pass < 2; ++pass) {
            for (size_t i = n; i-- > 0;) {
                if (keep[i] || !entry[key][i].is_string())
                    continue;
                const std::string row = "- " + entry[key][i].get<std::string>() + "\n";
                if ((pass == 0) != task_memo_scan::IsWriteLikeRow(row.substr(2)))
                    continue;
                if (used + row.size() > budget)
                    continue;
                keep[i] = 1;
                used += row.size();
            }
        }
        std::string result;
        for (size_t i = 0; i < n; ++i)
            if (keep[i])
                result += "- " + entry[key][i].get<std::string>() + "\n";
        return result.empty() ? result : head + result;
    };

    std::string header = "## Task Memo\n";

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

    std::string read_files_block;
    if (entry.contains("read_files") && entry["read_files"].is_object() && !entry["read_files"].empty())
        read_files_block = RenderReadFilesBlock(entry["read_files"], max_chars > 0 ? max_chars * 9 / 40 : 2200);

    const std::string pending_refs_block =
        entry.contains("pending_refs") ? task_memo_scan::RenderPendingRefs(entry["pending_refs"]) : std::string();

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

    std::string exec_cache_block;
    if (entry.contains("exec_cache") && entry["exec_cache"].is_object()
            && !entry["exec_cache"].empty()) {
        std::ostringstream oss;
        oss << "Idempotent read-only commands already known (reuse the result instead of re-running):\n";
        for (auto it = entry["exec_cache"].begin(); it != entry["exec_cache"].end(); ++it)
            if (it.value().is_string())
                oss << "- " << it.key() << " -> " << it.value().get<std::string>() << "\n";
        exec_cache_block = oss.str();
    }

    std::string artifacts_block;
    if (entry.contains("artifacts") && entry["artifacts"].is_object() && !entry["artifacts"].empty()) {
        std::ostringstream oss;
        oss << "Artifacts already produced, claimed by earlier tool calls from context that has since left "
               "the window (not an exhaustive list of everything done so far; entries marked (unverified) "
               "have not yet been re-checked by a read-only call; check the most recent messages below for "
               "newer results before redoing any of these steps):\n";
        for (auto it = entry["artifacts"].begin(); it != entry["artifacts"].end(); ++it)
            oss << "- " << task_memo_scan::RenderArtifactLine(it.key(), it.value()) << "\n";
        artifacts_block = oss.str();
    }

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
    const std::string page_directory_summary = (entry.contains("pages") && entry["pages"].is_array())
        ? task_memo_scan::RenderPageDirectorySummary(entry["pages"]) : std::string();
    const json tried_queries_count_safe = (entry.contains("tried_queries_count") && entry["tried_queries_count"].is_object())
        ? entry["tried_queries_count"] : json::object();
    const std::string tried_queries_summary = (entry.contains("tried_queries") && entry["tried_queries"].is_object())
        ? task_memo_scan::RenderTriedQueriesSummary(entry["tried_queries"], tried_queries_count_safe) : std::string();

    const std::string blocks[] = {
        render_list("Rules from documents you read (still in force)", "hard_rules"),
        pending_refs_block,
        render_list("Recovery episodes recorded from earlier context", "recovery_ledger"),
        artifacts_block,
        last_success_block,
        exec_cache_block,
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
        "hard_rules", "pending_refs", "recovery_ledger", "artifacts", "last_success", "exec_cache", "completed", "next_actions",
        "facts_constraints", "open_questions", "read_files", "tried_queries", "tool_state", "page_directory",
    };
    const size_t kBlockCount = sizeof(blocks) / sizeof(blocks[0]);

    std::string out = header;
    if (!goal_block.empty()) {
        if (max_chars > 0 && out.size() + goal_block.size() > max_chars) {

            size_t remaining = (max_chars > out.size()) ? (max_chars - out.size()) : 0;
            out += safe_utf8_truncate(goal_block, remaining, "");
        } else {
            out += goal_block;
        }
    }

    double reserve_ratio = entry.value("memo_reserve_ratio", 0.0);
    size_t budget_for_high_priority = max_chars;
    size_t reserved_for_memory = 0;
    if (max_chars > 0 && reserve_ratio > 0.0) {
        const size_t reserve_want = read_files_block.size() + tried_queries_block.size() + tool_state_block.size();
        const size_t reserve_cap = static_cast<size_t>(static_cast<double>(max_chars) * reserve_ratio);
        reserved_for_memory = std::min(reserve_want, reserve_cap);
        budget_for_high_priority = (max_chars > reserved_for_memory) ? (max_chars - reserved_for_memory) : 0;
    }

    auto degrade_block = [&](const char* block_name, size_t available) -> std::string {
        if (std::strcmp(block_name, "completed") == 0)
            return render_list_fit("Completed", "completed", available);
        if (std::strcmp(block_name, "tried_queries") == 0
                && !tried_queries_summary.empty() && tried_queries_summary.size() <= available)
            return tried_queries_summary;
        if (std::strcmp(block_name, "page_directory") == 0
                && !page_directory_summary.empty() && page_directory_summary.size() <= available)
            return page_directory_summary;
        return std::string();
    };

    std::vector<std::string> slot_text(kBlockCount);
    std::vector<std::string> layout_entries(kBlockCount);
    std::vector<size_t> skipped_slots;
    size_t out_used = out.size();

    for (size_t slot = 0; slot < kBlockCount; ++slot) {
        const std::string& block = blocks[slot];
        const char* block_name = kBlockNames[slot];
        if (block.empty()) {
            layout_entries[slot] = std::string(block_name) + ":0x ";
            continue;
        }
        const bool is_reserved_block = (std::strcmp(block_name, "read_files") == 0
                                         || std::strcmp(block_name, "tried_queries") == 0
                                         || std::strcmp(block_name, "tool_state") == 0);
        const size_t slot_budget = is_reserved_block ? max_chars : budget_for_high_priority;

        std::string candidate = block;
        if (max_chars > 0 && out_used + block.size() > slot_budget) {
            const size_t available = (slot_budget > out_used) ? (slot_budget - out_used) : 0;
            candidate = degrade_block(block_name, available);
        }

        if (candidate.empty()) {
            if (max_chars > 0)
                My_Log{My_Log::Level::kWarning}
                    << "[TaskMemo] Render block skipped: name=" << block_name
                    << ", block_chars=" << block.size()
                    << ", used_chars=" << out_used
                    << ", max_chars=" << max_chars
                    << ", slot_budget=" << slot_budget << std::endl;
            layout_entries[slot] = std::string(block_name) + ":" + std::to_string(block.size()) + "x ";
            skipped_slots.push_back(slot);
            continue;
        }
        slot_text[slot] = candidate;
        out_used += candidate.size();
        layout_entries[slot] = std::string(block_name) + ":" + std::to_string(candidate.size()) + " ";
    }

    if (max_chars > 0 && out_used < max_chars && !skipped_slots.empty()) {
        for (const size_t slot : skipped_slots) {
            const size_t available = (max_chars > out_used) ? (max_chars - out_used) : 0;
            const char* block_name = kBlockNames[slot];
            const std::string& full_block = blocks[slot];
            std::string candidate = (full_block.size() <= available) ? full_block : degrade_block(block_name, available);
            if (candidate.empty())
                continue;
            slot_text[slot] = candidate;
            out_used += candidate.size();
            layout_entries[slot] = std::string(block_name) + ":" + std::to_string(candidate.size()) + "+ ";
        }
    }

    std::string layout;
    for (size_t slot = 0; slot < kBlockCount; ++slot) {
        out += slot_text[slot];
        layout += layout_entries[slot];
    }
    if (max_chars > 0)
        My_Log{My_Log::Level::kInfo}
            << "[TaskMemo] Render layout: max_chars=" << max_chars
            << ", goal_chars=" << goal_block.size()
            << ", blocks=" << layout << "(x = skipped, + = admitted via reserve refill)" << std::endl;
    if (max_chars > 0 && out.size() > max_chars)
        out = safe_utf8_truncate(out, max_chars, "");
    return out;
}

static std::string RenderOutcomeClause(const std::string& outcome_preview, bool digest_stable)
{
    if (outcome_preview.empty())
        return "";
    return std::string(" Recorded result of the last attempt") +
           (digest_stable ? ", identical every time: \"" : ": \"") + outcome_preview + "\".";
}

static std::string RenderHardBlockForewarning(int repeat_count, bool digest_stable, int hard_block_threshold)
{
    if (!digest_stable || hard_block_threshold <= 0 || repeat_count + 1 < hard_block_threshold)
        return "";
    return " The next call with the same arguments will not be executed.";
}

std::string TaskMemoBuilder::RenderRedundantToolCallNote(int repeat_count, const std::string& outcome_preview,
                                                          bool digest_stable, int hard_block_threshold)
{
    std::ostringstream oss;
    oss << "Note: you have called the same tool with the same arguments " << repeat_count
        << " time(s) in a row." << RenderOutcomeClause(outcome_preview, digest_stable)
        << " Repeating it is unlikely to give new information: act on what you already know or try "
           "a different approach."
        << RenderHardBlockForewarning(repeat_count, digest_stable, hard_block_threshold) << "\n";
    return oss.str();
}

std::string TaskMemoBuilder::RenderNonContiguousRepeatNote(int repeat_count, const std::string& outcome_preview,
                                                            bool digest_stable, int hard_block_threshold)
{
    std::ostringstream oss;
    oss << "Note: you have called the same tool with the same arguments " << repeat_count
        << " time(s) so far in this task (not necessarily in a row)."
        << RenderOutcomeClause(outcome_preview, digest_stable)
        << " Repeating it is unlikely to give new information: act on what you already know or try "
           "a different approach."
        << RenderHardBlockForewarning(repeat_count, digest_stable, hard_block_threshold) << "\n";
    return oss.str();
}

std::string TaskMemoBuilder::BuildRereadFromTopNote(const std::string& read_ref, const json& read_files,
                                                     std::string& path_out)
{
    path_out.clear();
    if (read_ref.empty() || !read_files.is_object())
        return "";

    const size_t offset_pos = read_ref.find("#range:offset=");
    if (offset_pos != std::string::npos) {
        const unsigned long long offset_value =
            std::strtoull(read_ref.c_str() + offset_pos + strlen("#range:offset="), nullptr, 10);
        if (offset_value > 1)
            return "";
    }
    const std::string path = StripRangeSuffix(read_ref);
    if (path.empty() || !read_files.contains(path))
        return "";
    const json& file_entry = read_files[path];

    const std::vector<LineRange> seen = LoadLineRanges(file_entry.value("seen", json::array()));
    if (seen.empty())
        return "";
    std::ostringstream oss;
    oss << "Note: you are reading " << path << " from the beginning again, but lines "
        << JoinLineRanges(seen) << " of it were already shown earlier in this task.";
    const size_t next_offset = task_memo_scan::NextUnreadOffset(file_entry);
    if (next_offset > 0)
        oss << " The first part you have not seen yet starts at line " << next_offset << ".";
    oss << "\n";
    const std::string fact = task_memo_scan::RenderNearCursorFact(path, file_entry);
    if (!fact.empty())
        oss << fact;
    path_out = path;
    return oss.str();
}

bool TaskMemoBuilder::DetectOverlapReread(const json& file_entry, size_t first, size_t last,
                                           size_t& overlap_first, size_t& overlap_last)
{
    if (!file_entry.is_object() || first == 0 || last < first)
        return false;
    for (const auto& r : LoadLineRanges(file_entry.value("seen", json::array()))) {
        const size_t lo = std::max(r.first, first);
        const size_t hi = std::min(r.second, last);
        if (lo <= hi) {
            overlap_first = lo;
            overlap_last = hi;
            return true;
        }
    }
    return false;
}

bool TaskMemoBuilder::DetectRecentOverlapReread(const json& file_entry, size_t first, size_t last,
                                                 size_t current_seq, size_t recency_window,
                                                 size_t& overlap_first, size_t& overlap_last)
{
    if (!DetectOverlapReread(file_entry, first, last, overlap_first, overlap_last))
        return false;
    const size_t last_touch_seq = file_entry.value("_last_touch_seq", (size_t) 0);
    if (last_touch_seq == 0 || current_seq < last_touch_seq)
        return false;
    return (current_seq - last_touch_seq) <= recency_window;
}

std::string TaskMemoBuilder::BuildOverlapRereadNote(int overlap_count, int overlap_threshold,
                                                    const std::string& path, const std::string& range,
                                                    size_t next_offset)
{
    if (overlap_threshold <= 0 || overlap_count < overlap_threshold || path.empty() || range.empty())
        return "";

    std::ostringstream oss;
    oss << "Note: you re-read " << path << " at a non-zero offset, but lines " << range
        << " of it were already shown earlier in this task.";
    if (next_offset > 0)
        oss << " The first part you have not seen yet starts at line " << next_offset << ".";
    oss << " Act on what you already know, or read a part you have not seen yet.\n";
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
