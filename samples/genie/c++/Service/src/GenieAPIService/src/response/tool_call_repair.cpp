//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "response_tools.h"
#include "log.h"
#include "utils.h"
#include "../chat_request_handler/prompt_optimizer.h"
#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <limits>
#include <regex>
#include <unordered_map>
#include <vector>

std::string ResponseTools::convertToolCallJson(const std::string &input, ToolCallFailureReason *out_failure_reason,
                                               json *out_partial_recovery)
{
    if (out_failure_reason)
    {
        *out_failure_reason = ToolCallFailureReason::kNone;
    }
    if (out_partial_recovery)
    {
        *out_partial_recovery = json();
    }

    if (ResponseTools::log_inference_stream)
    {
        My_Log{My_Log::Level::kInfo} << "[DEBUG-TOOL-CALL] Raw model output (input):\n" << input << std::endl;
    }

    std::string jsonStr = extractJsonFromToolCall(input);

    std::string fixedJsonStr = fixBackslashes(jsonStr, true);

    if (ResponseTools::log_inference_stream)
    {
        My_Log{} << "[DEBUG-TOOL-CALL] ResponseTools::convertToolCallJson: \n" << wrapJsonInToolCall(fixedJsonStr) << std::endl;
    }

    json root;
    try
    {
        root = json::parse(fixedJsonStr);
    }
    catch (const std::exception &e)
    {

        My_Log{} << "[DEBUG-TOOL-CALL] Outer JSON parse failed (" << e.what() << "), trying repairJson..." << std::endl;
        std::string repairedJsonStr = repairJson(fixedJsonStr);
        try
        {
            root = json::parse(repairedJsonStr);
        }
        catch (const std::exception &e2)
        {

            My_Log{} << "[DEBUG-TOOL-CALL] repairJson failed (" << e2.what()
                     << "), trying to escape literal control chars in string values..." << std::endl;
            std::string ctrlFixedStr = escapeControlCharsInJsonStrings(repairedJsonStr);
            try
            {
                root = json::parse(ctrlFixedStr);
            }
            catch (const std::exception &e3)
            {
                My_Log{My_Log::Level::kError} << "parse tool calls's message as json failed:" << e3.what() << std::endl;

                json layer1_result;
                ToolCallFailureReason layer1_reason = ToolCallFailureReason::kUnparseable;
                if (TryLayer1Recovery(jsonStr, layer1_result, layer1_reason, out_partial_recovery))
                {
                    My_Log{My_Log::Level::kInfo}
                        << "[Layer1Recovery] Recovered tool call from malformed output: "
                        << layer1_result.dump() << std::endl;
                    root = layer1_result;
                    goto done;
                }

                My_Log{My_Log::Level::kWarning}
                    << "[Layer1Recovery] Failed to recover ("
                    << ToolCallFailureReasonToString(layer1_reason) << "), falling back to unknow." << std::endl;
                if (out_failure_reason)
                {
                    *out_failure_reason = layer1_reason;
                }
                root["name"] = "unknow";
                root["arguments"] = fixedJsonStr;
                goto done;
            }
        }
    }

    if (!root.contains("name") || !root["name"].is_string() || root["name"].get<std::string>().empty())
    {
        My_Log{My_Log::Level::kError} << "the tool calls's name as string failed\n";
        root["name"] = "unknow";
        if (out_failure_reason)
        {
            *out_failure_reason = ToolCallFailureReason::kUnknownToolName;
        }
    }
    else if (ResponseTools::log_inference_stream)
    {
        My_Log{My_Log::Level::kInfo} << "[DEBUG-TOOL-CALL] Parsed tool name: " << root["name"].get<std::string>() << std::endl;
    }

    if (root.contains("arguments") && root["arguments"].is_string())
    {
        std::string args_str = root["arguments"].get<std::string>();

        std::string repaired_args = repairJson(args_str);

        try
        {
            json args_obj = json::parse(repaired_args);
            if (args_obj.is_object())
            {

                if (ResponseTools::log_inference_stream)
                {
                    My_Log{My_Log::Level::kInfo}
                        << "[DEBUG-TOOL-CALL] arguments string converted to object:\n" << args_obj.dump(2) << std::endl;
                }
                root["arguments"] = args_obj;
            }
        }
        catch (const std::exception &e)
        {

            My_Log{} << "Arguments parse failed (" << e.what() << "), trying to fix..." << std::endl;

            std::string ctrl_fixed;
            ctrl_fixed.reserve(args_str.size() * 2);
            for (size_t i = 0; i < args_str.size(); ++i) {
                unsigned char c = static_cast<unsigned char>(args_str[i]);
                if (c == '\n') {
                    ctrl_fixed += "\\n";
                } else if (c == '\r') {
                    ctrl_fixed += "\\r";
                } else if (c == '\t') {
                    ctrl_fixed += "\\t";
                } else if (c < 0x20) {
                    char buf[8];
                    snprintf(buf, sizeof(buf), "\\u%04x", static_cast<unsigned int>(c));
                    ctrl_fixed += buf;
                } else {
                    ctrl_fixed += args_str[i];
                }
            }

            try
            {
                json args_obj = json::parse(ctrl_fixed);
                if (args_obj.is_object())
                {
                    My_Log{} << "Successfully fixed control chars and parsed arguments" << std::endl;
                    root["arguments"] = args_obj;
                }
            }
            catch (const std::exception &e2)
            {

                My_Log{} << "Control char fix failed (" << e2.what() << "), trying backslash fix..." << std::endl;

                std::string fixed_args_str = fixBackslashes(args_str, false);

                try
                {
                    json args_obj = json::parse(fixed_args_str);
                    if (args_obj.is_object())
                    {
                        My_Log{} << "Successfully fixed backslashes and parsed arguments" << std::endl;
                        root["arguments"] = args_obj;
                    }
                }
                catch (const std::exception &e3)
                {

                    My_Log{} << "Arguments remains as string (all fixes failed): " << e3.what() << std::endl;
                }
            }
        }
    }

    if (!root.contains("arguments") || (!root["arguments"].is_object() && !root["arguments"].is_string()))
    {
        My_Log{My_Log::Level::kError} << "parse tool calls's args as json failed:" << std::endl;
        root["arguments"] = jsonStr;
        goto done;
    }

    done:
    std::string final_result = root.dump();
    if (ResponseTools::log_inference_stream)
    {
        My_Log{My_Log::Level::kInfo} << "[DEBUG-TOOL-CALL] Final JSON:\n" << final_result << std::endl;
    }

    return wrapJsonInToolCall(final_result);
}

std::string ResponseTools::extractJsonFromToolCall(const std::string &input)
{
    std::string output = str_replace(input, "<tool_call>", "");
    output = str_replace(output, "</tool_call>", "");
    output = str_replace(output, "[/tool_call]", "");
    return output;
}

static size_t skipJsonString(const std::string &s, size_t pos)
{
    if (pos >= s.size() || s[pos] != '"') return pos;
    ++pos;
    while (pos < s.size())
    {
        if (s[pos] == '\\' && pos + 1 < s.size())
        {
            pos += 2;
        }
        else if (s[pos] == '"')
        {
            ++pos;
            break;
        }
        else
        {
            ++pos;
        }
    }
    return pos;
}

static std::string repairJsonOutsideStrings(const std::string &input)
{
    std::string result;
    result.reserve(input.size());

    auto isWordChar = [](char c) -> bool {
        return std::isalnum(static_cast<unsigned char>(c)) || c == '_';
    };

    auto lastSignificant = [&result]() -> char {
        for (size_t k = result.size(); k > 0; --k)
        {
            char ch = result[k - 1];
            if (ch != ' ' && ch != '\t' && ch != '\n' && ch != '\r') return ch;
        }
        return '\0';
    };

    size_t i = 0;
    while (i < input.size())
    {
        bool missingOpenQuote = input[i] == '\\' && i + 1 < input.size() && input[i + 1] == '"'
                                && (lastSignificant() == ':' || lastSignificant() == '[' || lastSignificant() == ',');
        if (input[i] == '"' || missingOpenQuote)
        {

            if (missingOpenQuote) result += '"';
            else result += input[i++];
            while (i < input.size())
            {
                if (input[i] == '\\' && i + 1 < input.size())
                {

                    result += input[i];
                    result += input[i + 1];
                    i += 2;
                }
                else if (input[i] == '"')
                {
                    result += input[i++];
                    break;
                }
                else
                {
                    result += input[i++];
                }
            }
        }
        else if (input[i] == ',')
        {

            size_t j = i + 1;
            while (j < input.size() && (input[j] == ' ' || input[j] == '\t' ||
                                         input[j] == '\n' || input[j] == '\r'))
                ++j;
            if (j < input.size() && (input[j] == '}' || input[j] == ']'))
            {
                i++;
            }

            else if (j < input.size() && input[j] == '"')
            {

                size_t str_end = skipJsonString(input, j);

                bool is_empty_str = (str_end == j + 2);
                if (is_empty_str)
                {

                    size_t k = str_end;
                    while (k < input.size() && (input[k] == ' ' || input[k] == '\t' ||
                                                 input[k] == '\n' || input[k] == '\r'))
                        ++k;
                    if (k < input.size() && (input[k] == ',' || input[k] == '}' || input[k] == ']'))
                    {

                        i = str_end;
                        continue;
                    }
                }

                result += input[i++];
            }
            else
            {
                result += input[i++];
            }
        }
        else
        {

            bool leftBound = (i == 0) || !isWordChar(input[i - 1]);

            if (leftBound)
            {

                if (i + 4 <= input.size() && input.compare(i, 4, "None") == 0)
                {
                    bool rightBound = (i + 4 >= input.size()) || !isWordChar(input[i + 4]);
                    if (rightBound) { result += "null";  i += 4; continue; }
                }

                if (i + 4 <= input.size() && input.compare(i, 4, "True") == 0)
                {
                    bool rightBound = (i + 4 >= input.size()) || !isWordChar(input[i + 4]);
                    if (rightBound) { result += "true";  i += 4; continue; }
                }

                if (i + 5 <= input.size() && input.compare(i, 5, "False") == 0)
                {
                    bool rightBound = (i + 5 >= input.size()) || !isWordChar(input[i + 5]);
                    if (rightBound) { result += "false"; i += 5; continue; }
                }
            }

            result += input[i++];
        }
    }
    return result;
}

std::string ResponseTools::repairJson(const std::string &input)
{

    return repairJsonOutsideStrings(input);
}

bool ResponseTools::IsSkillName(const std::string& tool_name, const std::unordered_map<std::string, std::string>& skill_mappings) {
    return skill_mappings.find(tool_name) != skill_mappings.end();
}

std::string ResponseTools::RewriteToReadCall(
    const std::string& skill_name,
    const std::unordered_map<std::string, std::string>& skill_mappings
) {
    auto it = skill_mappings.find(skill_name);
    if (it == skill_mappings.end()) {
        return "";
    }

    json rewritten_call = {
        {"name", "read"},
        {"arguments", {
            {"path", it->second}
        }}
    };

    return rewritten_call.dump();
}

std::string ResponseTools::AutoCorrectSkillCall(
    const std::string& tool_call_json_str,
    const std::unordered_map<std::string, std::string>& skill_mappings,
    bool enable_correction
) {
    if (!enable_correction || skill_mappings.empty()) {
        return tool_call_json_str;
    }

    bool has_tool_call_tag = (tool_call_json_str.find("<tool_call>") != std::string::npos);
    std::string json_str = has_tool_call_tag
        ? extractJsonFromToolCall(tool_call_json_str)
        : tool_call_json_str;

    size_t start = json_str.find_first_not_of(" \t\r\n");
    size_t end   = json_str.find_last_not_of(" \t\r\n");
    if (start != std::string::npos) {
        json_str = json_str.substr(start, end - start + 1);
    }

    try {

        json tool_call = json::parse(json_str);

        if (!tool_call.contains("name") || !tool_call["name"].is_string()) {
            return tool_call_json_str;
        }

        std::string tool_name = tool_call["name"].get<std::string>();
        bool modified = false;

        {
            std::string matched_skill_id;
            if (IsSkillName(tool_name, skill_mappings)) {

                matched_skill_id = tool_name;
            } else {

                std::string prefixed = "skill_" + tool_name;
                if (IsSkillName(prefixed, skill_mappings)) {
                    matched_skill_id = prefixed;
                }
            }

            if (!matched_skill_id.empty()) {
                My_Log{My_Log::Level::kWarning}
                    << "[Auto-Correction] Model incorrectly called skill '" << tool_name
                    << "' as a tool (matched skill_id='" << matched_skill_id
                    << "'). Rewriting to read(SKILL.md)." << std::endl;

                std::string rewritten = RewriteToReadCall(matched_skill_id, skill_mappings);
                if (!rewritten.empty()) {

                    return has_tool_call_tag ? wrapJsonInToolCall(rewritten) : rewritten;
                }
            }
        }

        if (tool_call.contains("arguments") && tool_call["arguments"].is_object()) {
            auto& args = tool_call["arguments"];

            if (args.contains("file_path") && !args.contains("path")) {
                My_Log{My_Log::Level::kWarning}
                    << "[Auto-Correction] Tool '" << tool_name
                    << "' used legacy 'file_path' instead of 'path'. Auto-fixing." << std::endl;
                args["path"] = args["file_path"];
                args.erase("file_path");
                modified = true;
            }

            if (tool_name == "edit" && !args.contains("edits") &&
                args.contains("oldText") && args.contains("newText")) {
                My_Log{My_Log::Level::kWarning}
                    << "[Auto-Correction] edit tool used legacy flat oldText/newText format. "
                    << "Converting to edits array." << std::endl;
                json edit_item = {
                    {"oldText", args["oldText"]},
                    {"newText", args["newText"]}
                };
                args["edits"] = json::array({edit_item});
                args.erase("oldText");
                args.erase("newText");
                modified = true;
            }
        }

        else if (tool_call.contains("arguments") && tool_call["arguments"].is_string()) {
            std::string args_str = tool_call["arguments"].get<std::string>();
            try {
                json args_obj = json::parse(args_str);
                if (args_obj.is_object()) {
                    bool inner_modified = false;

                    if (args_obj.contains("file_path") && !args_obj.contains("path")) {
                        My_Log{My_Log::Level::kWarning}
                            << "[Auto-Correction] Tool '" << tool_name
                            << "' used legacy 'file_path' instead of 'path' (string args). Auto-fixing." << std::endl;
                        args_obj["path"] = args_obj["file_path"];
                        args_obj.erase("file_path");
                        inner_modified = true;
                    }

                    if (tool_name == "edit" && !args_obj.contains("edits") &&
                        args_obj.contains("oldText") && args_obj.contains("newText")) {
                        My_Log{My_Log::Level::kWarning}
                            << "[Auto-Correction] edit tool used legacy flat oldText/newText format "
                            << "(string args). Converting to edits array." << std::endl;
                        json edit_item = {
                            {"oldText", args_obj["oldText"]},
                            {"newText", args_obj["newText"]}
                        };
                        args_obj["edits"] = json::array({edit_item});
                        args_obj.erase("oldText");
                        args_obj.erase("newText");
                        inner_modified = true;
                    }

                    if (inner_modified) {
                        tool_call["arguments"] = args_obj.dump();
                        modified = true;
                    }
                }
            } catch (const json::exception&) {

            }
        }

        if (modified) {
            std::string fixed_json = tool_call.dump();
            return has_tool_call_tag ? wrapJsonInToolCall(fixed_json) : fixed_json;
        }

        return tool_call_json_str;

    } catch (const json::exception& e) {
        My_Log{My_Log::Level::kError}
            << "[Auto-Correction] JSON parse error: " << e.what() << std::endl;
        return tool_call_json_str;
    }
}

std::string ResponseTools::escapeControlCharsInJsonStrings(const std::string &input)
{
    std::string result;
    result.reserve(input.size() * 2);

    bool in_string = false;

    for (size_t i = 0; i < input.size(); ++i)
    {
        unsigned char c = static_cast<unsigned char>(input[i]);

        if (!in_string)
        {

            if (c == '"')
            {
                in_string = true;
            }
            result += input[i];
        }
        else
        {

            if (c == '\\' && i + 1 < input.size())
            {

                result += input[i];
                result += input[i + 1];
                ++i;
            }
            else if (c == '"')
            {

                in_string = false;
                result += input[i];
            }
            else if (c == '\n')
            {
                result += "\\n";
            }
            else if (c == '\r')
            {
                result += "\\r";
            }
            else if (c == '\t')
            {
                result += "\\t";
            }
            else if (c < 0x20)
            {

                char buf[8];
                snprintf(buf, sizeof(buf), "\\u%04x", static_cast<unsigned int>(c));
                result += buf;
            }
            else
            {
                result += input[i];
            }
        }
    }
    return result;
}

std::string ResponseTools::fixBackslashes(const std::string &input, bool smart_mode)
{
    std::string result;
    result.reserve(input.size() * 2);

    if (smart_mode)
    {

        for (size_t i = 0; i < input.size(); ++i) {
            if (input[i] == '\\') {
                if (i + 1 < input.size()) {
                    char next = input[i + 1];

                    if (next == '"' || next == '\\' || next == '/' ||
                        next == 'b' || next == 'f' || next == 'n' ||
                        next == 'r' || next == 't' || next == 'u') {
                        result += input[i];
                        result += input[i + 1];
                        ++i;
                    } else {

                        result += "\\\\";
                    }
                } else {

                    result += "\\\\";
                }
            } else {
                result += input[i];
            }
        }
    }
    else
    {

        for (size_t i = 0; i < input.size(); ++i) {
            if (input[i] == '\\') {
                result += "\\\\";
            } else {
                result += input[i];
            }
        }
    }

    return result;
}

namespace {

struct Layer1Candidate
{
    std::string text;
    bool closed_properly = false;
    size_t start_pos = 0;
};

std::string ToLowerAscii(const std::string &s)
{
    std::string result = s;
    std::transform(result.begin(), result.end(), result.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return result;
}

std::string TrimWhitespace(const std::string &s)
{
    size_t start = s.find_first_not_of(" \t\r\n");
    if (start == std::string::npos)
    {
        return "";
    }
    size_t end = s.find_last_not_of(" \t\r\n");
    return s.substr(start, end - start + 1);
}

std::vector<Layer1Candidate> ExtractBalancedJsonCandidates(const std::string &text)
{
    std::vector<Layer1Candidate> candidates;
    const size_t n = text.size();
    size_t i = 0;

    while (i < n)
    {
        if (text[i] != '{')
        {
            ++i;
            continue;
        }

        size_t start = i;
        int depth = 0;
        bool in_string = false;
        bool closed = false;
        size_t j = i;

        for (; j < n; ++j)
        {
            char c = text[j];
            if (in_string)
            {
                if (c == '\\' && j + 1 < n)
                {
                    ++j;
                }
                else if (c == '"')
                {
                    in_string = false;
                }
                continue;
            }

            if (c == '"')
            {
                in_string = true;
            }
            else if (c == '{')
            {
                ++depth;
            }
            else if (c == '}')
            {
                --depth;
                if (depth == 0)
                {
                    closed = true;
                    ++j;
                    break;
                }
            }
        }

        Layer1Candidate candidate;
        candidate.text = text.substr(start, j - start);
        candidate.closed_properly = closed;
        candidate.start_pos = start;
        candidates.push_back(candidate);

        if (!closed)
        {

            break;
        }
        i = j;
    }

    return candidates;
}

bool TryParseLayer1Candidate(const Layer1Candidate &candidate, json &out_parsed, bool &out_truncated_signal)
{
    out_truncated_signal = false;

    auto try_parse = [](const std::string &s, json &out) -> bool {
        try
        {
            out = json::parse(s);
            return out.is_object();
        }
        catch (...)
        {
            return false;
        }
    };

    if (try_parse(candidate.text, out_parsed))
    {
        return true;
    }

    std::string repaired = ResponseTools::repairJson(candidate.text);
    if (try_parse(repaired, out_parsed))
    {
        return true;
    }

    if (candidate.closed_properly)
    {

        return false;
    }

    bool in_string = false;
    int open_depth = 0;
    for (size_t k = 0; k < repaired.size(); ++k)
    {
        char c = repaired[k];
        if (in_string)
        {
            if (c == '\\' && k + 1 < repaired.size())
            {
                ++k;
            }
            else if (c == '"')
            {
                in_string = false;
            }
            continue;
        }
        if (c == '"') in_string = true;
        else if (c == '{') ++open_depth;
        else if (c == '}') --open_depth;
    }

    std::string padded = repaired;
    if (in_string)
    {
        padded += "\"";
        out_truncated_signal = true;
    }
    if (open_depth > 0)
    {
        padded.append(static_cast<size_t>(open_depth), '}');
        out_truncated_signal = true;
    }

    if (!out_truncated_signal)
    {

        return false;
    }

    return try_parse(padded, out_parsed);
}

bool ExtractNameField(const json &obj, std::string &out_name)
{
    if (!obj.is_object())
    {
        return false;
    }
    static const char *kNameKeys[] = {"name", "tool", "tool_name"};
    for (const char *key : kNameKeys)
    {
        if (obj.contains(key) && obj[key].is_string() && !obj[key].get<std::string>().empty())
        {
            out_name = obj[key].get<std::string>();
            return true;
        }
    }
    if (obj.contains("function") && obj["function"].is_object())
    {
        const auto &fn = obj["function"];
        if (fn.contains("name") && fn["name"].is_string() && !fn["name"].get<std::string>().empty())
        {
            out_name = fn["name"].get<std::string>();
            return true;
        }
    }
    return false;
}

json ExtractArgumentsField(const json &obj)
{
    static const char *kArgKeys[] = {"arguments", "params", "parameters", "args"};
    for (const char *key : kArgKeys)
    {
        if (obj.contains(key))
        {
            return obj[key];
        }
    }
    if (obj.contains("function") && obj["function"].is_object() && obj["function"].contains("arguments"))
    {
        return obj["function"]["arguments"];
    }
    return json::object();
}

std::string NormalizeAndMatchToolName(const std::string &raw_name)
{
    std::string lower = ToLowerAscii(TrimWhitespace(raw_name));
    if (lower.empty())
    {
        return "";
    }

    const auto &signatures = PromptOptimizer::GetKnownToolSignatures();

    for (const auto &entry : signatures)
    {
        if (entry.first == lower)
        {
            return entry.first;
        }
    }

    auto levenshtein = [](const std::string &a, const std::string &b) -> size_t {
        const size_t la = a.size();
        const size_t lb = b.size();
        if (la == 0) return lb;
        if (lb == 0) return la;

        std::vector<size_t> prev(lb + 1);
        std::vector<size_t> curr(lb + 1);
        for (size_t j = 0; j <= lb; ++j) prev[j] = j;
        for (size_t i = 1; i <= la; ++i)
        {
            curr[0] = i;
            for (size_t j = 1; j <= lb; ++j)
            {
                size_t cost = (a[i - 1] == b[j - 1]) ? 0 : 1;
                curr[j] = std::min({prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost});
            }
            std::swap(prev, curr);
        }
        return prev[lb];
    };

    std::string best_match;
    size_t best_distance = std::numeric_limits<size_t>::max();
    for (const auto &entry : signatures)
    {
        const std::string &known = entry.first;
        if (std::abs(static_cast<long long>(known.size()) - static_cast<long long>(lower.size())) > 2)
        {
            continue;
        }
        size_t distance = levenshtein(lower, known);

        size_t threshold = (known.size() <= 4) ? 0 : 2;
        if (distance <= threshold && distance < best_distance)
        {
            best_distance = distance;
            best_match = known;
        }
    }
    return best_match;
}

void NormalizeArgumentKeys(json &args)
{
    if (!args.is_object())
    {
        return;
    }

    static const std::unordered_map<std::string, std::string> kArgKeyAliases = {
        {"file_path",    "path"},
        {"filepath",     "path"},
        {"cmd",          "command"},
        {"cmdline",      "command"},
        {"command_line", "command"},
        {"file_content", "content"},
        {"search_query", "query"},
        {"target_url",   "url"},
    };

    json normalized = json::object();
    for (auto it = args.begin(); it != args.end(); ++it)
    {
        std::string lower_key = ToLowerAscii(it.key());
        auto alias_it = kArgKeyAliases.find(lower_key);
        std::string final_key = (alias_it != kArgKeyAliases.end()) ? alias_it->second : it.key();

        if (!normalized.contains(final_key))
        {
            normalized[final_key] = it.value();
        }
    }
    args = normalized;
}

std::vector<std::string> SplitTopLevelCommas(const std::string &blob)
{
    std::vector<std::string> parts;
    int depth = 0;
    std::string current;
    for (char c : blob)
    {
        if (c == '[' || c == '{' || c == '(')
        {
            ++depth;
            current += c;
        }
        else if (c == ']' || c == '}' || c == ')')
        {
            --depth;
            current += c;
        }
        else if (c == ',' && depth == 0)
        {
            parts.push_back(current);
            current.clear();
        }
        else
        {
            current += c;
        }
    }
    if (!current.empty() || !parts.empty())
    {
        parts.push_back(current);
    }
    return parts;
}

std::vector<std::string> ParseRequiredParamsFromSignature(const std::string &signature)
{
    std::vector<std::string> required;
    size_t open = signature.find('(');
    size_t close = signature.rfind(')');
    if (open == std::string::npos || close == std::string::npos || close <= open)
    {
        return required;
    }

    std::string blob = signature.substr(open + 1, close - open - 1);
    for (const auto &raw_token : SplitTopLevelCommas(blob))
    {
        std::string token = TrimWhitespace(raw_token);
        if (token.empty() || token == "...")
        {
            continue;
        }
        bool optional = (token.back() == '?');
        size_t name_end = token.find_first_of(":?");
        std::string name = TrimWhitespace((name_end == std::string::npos) ? token : token.substr(0, name_end));
        if (!name.empty() && !optional)
        {
            required.push_back(name);
        }
    }
    return required;
}

bool IsEmptyArgValue(const json &value)
{
    if (value.is_null())
    {
        return true;
    }
    if (value.is_string())
    {
        return value.get<std::string>().empty();
    }
    if (value.is_array() || value.is_object())
    {
        return value.empty();
    }
    return false;
}

std::vector<std::string> ComputeMissingRequiredArgs(const std::string &tool_name, const json &args)
{
    std::vector<std::string> missing;
    const auto &signatures = PromptOptimizer::GetKnownToolSignatures();
    auto it = signatures.find(tool_name);
    if (it == signatures.end())
    {
        return missing;
    }
    for (const auto &name : ParseRequiredParamsFromSignature(it->second))
    {
        if (!args.is_object() || !args.contains(name) || IsEmptyArgValue(args[name]))
        {
            missing.push_back(name);
        }
    }
    return missing;
}

std::string StripLeadingTrailingCodeFence(const std::string &text)
{
    std::string s = TrimWhitespace(text);
    if (s.compare(0, 3, "```") == 0)
    {
        size_t newline = s.find('\n');
        if (newline != std::string::npos)
        {
            std::string first_line = ToLowerAscii(TrimWhitespace(s.substr(3, newline - 3)));
            if (first_line.empty() || first_line == "json")
            {
                s = TrimWhitespace(s.substr(newline + 1));
            }
        }
    }
    if (s.size() >= 3 && s.compare(s.size() - 3, 3, "```") == 0)
    {
        s = TrimWhitespace(s.substr(0, s.size() - 3));
    }
    return s;
}

bool LooksLikeBareToolCallCandidate(const json &parsed, std::string &out_matched_tool)
{
    std::string raw_name;
    if (!ExtractNameField(parsed, raw_name))
    {
        return false;
    }
    std::string matched = NormalizeAndMatchToolName(raw_name);
    if (matched.empty())
    {
        return false;
    }
    static const char *kArgKeys[] = {"arguments", "params", "parameters", "args"};
    bool has_args_field = false;
    for (const char *key : kArgKeys)
    {
        if (parsed.contains(key))
        {
            has_args_field = true;
            break;
        }
    }
    if (!has_args_field && parsed.contains("function") && parsed["function"].is_object()
        && parsed["function"].contains("arguments"))
    {
        has_args_field = true;
    }
    if (!has_args_field)
    {
        return false;
    }
    out_matched_tool = matched;
    return true;
}

}

bool ResponseTools::TryLayer1Recovery(const std::string &malformedText, json &out_tool_call, ToolCallFailureReason &out_reason,
                                      json *out_partial_recovery)
{
    out_reason = ToolCallFailureReason::kUnparseable;
    if (out_partial_recovery)
    {
        *out_partial_recovery = json();
    }

    std::vector<Layer1Candidate> candidates = ExtractBalancedJsonCandidates(malformedText);
    if (candidates.empty())
    {
        return false;
    }

    struct ParsedCandidate
    {
        json obj;
        std::string raw_name;
    };
    std::vector<ParsedCandidate> reasonable;
    bool saw_truncation_signal = false;

    for (const auto &candidate : candidates)
    {
        json parsed;
        bool truncated_signal = false;
        if (!TryParseLayer1Candidate(candidate, parsed, truncated_signal))
        {
            saw_truncation_signal = saw_truncation_signal || truncated_signal || !candidate.closed_properly;
            continue;
        }

        std::string raw_name;
        if (ExtractNameField(parsed, raw_name))
        {
            reasonable.push_back({parsed, raw_name});
        }
        else
        {

            saw_truncation_signal = saw_truncation_signal || truncated_signal || !candidate.closed_properly;
        }
    }

    if (reasonable.size() >= 2)
    {
        out_reason = ToolCallFailureReason::kAmbiguousMultipleCalls;
        return false;
    }

    if (reasonable.empty())
    {
        out_reason = saw_truncation_signal ? ToolCallFailureReason::kTruncated : ToolCallFailureReason::kUnparseable;

        if (out_partial_recovery)
        {

            static const std::regex kNameFieldSalvage(R"RX("name"\s*:\s*"([A-Za-z_]\w*)")RX");
            std::smatch match;
            if (std::regex_search(malformedText, match, kNameFieldSalvage))
            {
                std::string salvaged_tool = NormalizeAndMatchToolName(match[1].str());
                if (!salvaged_tool.empty())
                {
                    json partial = json::object();
                    partial["name"] = salvaged_tool;
                    partial["arguments"] = json{{"_parse_error",
                        "arguments were not valid JSON; re-emit the call with every string value quoted and "
                        "\\\" \\\\ newlines escaped inside strings"}};
                    *out_partial_recovery = partial;
                }
            }
        }
        return false;
    }

    const ParsedCandidate &picked = reasonable.front();
    std::string matched_tool = NormalizeAndMatchToolName(picked.raw_name);
    if (matched_tool.empty())
    {
        out_reason = ToolCallFailureReason::kUnknownToolName;
        return false;
    }

    json args = ExtractArgumentsField(picked.obj);
    if (args.is_string())
    {

        try
        {
            args = json::parse(args.get<std::string>());
        }
        catch (...)
        {
            args = json::object();
        }
    }
    if (!args.is_object())
    {
        args = json::object();
    }
    NormalizeArgumentKeys(args);

    std::vector<std::string> missing_required_args = ComputeMissingRequiredArgs(matched_tool, args);
    if (!missing_required_args.empty())
    {
        out_reason = ToolCallFailureReason::kMissingRequiredArgs;

        if (out_partial_recovery)
        {
            json best_effort_args = args;
            for (const auto &missing_name : missing_required_args)
            {
                best_effort_args[missing_name] = "";
            }
            json partial = json::object();
            partial["name"] = matched_tool;
            partial["arguments"] = best_effort_args;
            *out_partial_recovery = partial;
        }
        return false;
    }

    out_tool_call = json::object();
    out_tool_call["name"] = matched_tool;
    out_tool_call["arguments"] = args;
    out_reason = ToolCallFailureReason::kNone;
    return true;
}

bool ResponseTools::DetectBareToolCall(const std::string &visible_text, std::string &out_wrapped)
{
    std::string text = StripLeadingTrailingCodeFence(visible_text);
    if (text.empty() || text.find('{') == std::string::npos)
    {
        return false;
    }

    std::vector<Layer1Candidate> candidates = ExtractBalancedJsonCandidates(text);
    if (candidates.empty())
    {
        return false;
    }

    size_t first_start = std::string::npos;
    size_t last_end = 0;
    int reasonable_count = 0;

    for (const auto &candidate : candidates)
    {
        json parsed;
        bool truncated_signal = false;
        if (!TryParseLayer1Candidate(candidate, parsed, truncated_signal))
        {
            continue;
        }
        std::string matched_tool;
        if (!LooksLikeBareToolCallCandidate(parsed, matched_tool))
        {
            continue;
        }

        ++reasonable_count;
        if (first_start == std::string::npos)
        {
            first_start = candidate.start_pos;
        }
        last_end = candidate.start_pos + candidate.text.size();
    }

    if (reasonable_count == 0)
    {

        return false;
    }

    std::string prefix = TrimWhitespace(text.substr(0, first_start));
    std::string suffix = TrimWhitespace(text.substr(last_end));
    size_t prose_len = prefix.size() + suffix.size();
    size_t candidate_span_len = last_end - first_start;
    static constexpr size_t kProseAbsoluteAllowance = 80;
    if (prose_len > candidate_span_len && prose_len > kProseAbsoluteAllowance)
    {
        return false;
    }

    out_wrapped = ResponseTools::wrapJsonInToolCall(text.substr(first_start, candidate_span_len));
    return true;
}
