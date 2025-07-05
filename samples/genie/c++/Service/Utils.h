//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#ifndef _UTILS_H
#define _UTILS_H

#ifndef FMT_HEADER_ONLY
#define FMT_HEADER_ONLY
#endif

#include "httplib.h"
#include "nlohmann/json.hpp"
#include "fmt/core.h"
#include "cmdparser.hpp"
#include <filesystem>
#include <chrono>
#include <random>
#include <vector>
#include <unordered_map>
#include <codecvt>
#include <locale>
#include <io.h>
#include <fcntl.h>

using namespace httplib;
using namespace std;
using json = nlohmann::ordered_json;
namespace fs = std::filesystem;

const std::string HOST = "0.0.0.0";
const std::string BLANK_STRING = std::string("");
const int CONTEXT_SIZE = 4096;
const int DOCS_MAX_SIZE = CONTEXT_SIZE - 1024;
const int DOCS_MAX_QUERY_TIMES = 3;
const std::vector<std::string>& SEPARATORS = {"\n\n", "\n", "。", "！", "？", "，", ".", "?", "!", ",", " ", ""};

const std::string FN_FLAG = "<";
const std::string FN_NAME = "<tool_call>";
const std::string FILL_THINK = "<think>\n\n</think>\n\n";

#define RED "\033[31m"
#define GREEN "\033[32m"
#define YELLOW "\033[33m"
#define BLUE "\033[34m"
#define MAGENTA "\033[35m"
#define CYAN "\033[36m"
#define BOLD "\033[1m"
#define UNDERLINE "\033[4m"
#define ITALIC "\033[3m"
#define RESET "\033[0m"
#define MIMETYPE_JSON "application/json; charset=utf-8"

const auto root_html = R"(
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="UTF-8">
    <title>Genie OpenAI API Service</title>
    <style>
    body { word-wrap: break-word; white-space: normal; }
    h1 {text-align: center;}
    </style>
    </head>
    <body> 
    <br><br><br><br><br><br><br><br><br><br>
    <h1>Genie OpenAI API Service IS Running.</h1>
    </body>
    </html>
    )";

inline const std::string tool_prompt_template = 
    "\n\n# Tools\n\n"
    "You may call one or more functions to assist with the user query.\n\n"
    "You are provided with function signatures within <tools></tools> XML tags:\n"
    "<tools>\n"
    "{tool_descs}"
    "</tools>\n\n"
    "For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:\n"
    "<tool_call>\n"
    "{\"name\": <function-name>, \"arguments\": <args-json-object>}\n"
    "</tool_call>\n";

#ifdef __ANDROID__
#include <android/log.h>
#define LOG_TAG "com.example.genieapiservice"
#define LOGD(...) __android_log_print(ANDROID_LOG_DEBUG, LOG_TAG, __VA_ARGS__)
void My_Log(const std::string& message) {
    LOGD("%s", message.c_str());
}
#else
void My_Log(const std::string& message) {
    std::cout << message << std::endl;
}
#endif

template<typename T>
T get_json_value(const json& jsonData, const std::string& key, const T& defaultValue) {
    try {
        if (jsonData.contains(key)) {
            return jsonData[key].get<T>();
        }
    } catch (const std::exception& e) {
        std::cerr << "Error getting JSON value for key '" << key << "': " << e.what() << std::endl;
    }

    return defaultValue;
}

static std::string json_to_str(const json & data) {
    return data.dump(-1, ' ', false, json::error_handler_t::replace);
}

void print_wstring(const std::string message) {
#ifdef _WIN32
    _setmode(_fileno(stdout), _O_U16TEXT);

    std::wstring_convert<std::codecvt_utf8_utf16<wchar_t>> converter;
    std::wstring wmsg = converter.from_bytes(message);
    std::wcout << wmsg << std::flush;

    _setmode(_fileno(stdout), _O_TEXT);
#else
    std::cout << message << std::flush;
#endif
}

bool starts_with(const std::string& str, const std::string& prefix) {
    return str.rfind(prefix, 0) == 0;
}

bool str_contains(const std::string& str, const std::string& sub) {
    return str.find(sub) != std::string::npos;
}

std::string trim_empty_lines(const std::string& input) {
    std::string s = input;
    s = std::regex_replace(s, std::regex(R"(^(\s*\n)+)"), "");
    s = std::regex_replace(s, std::regex(R"((\s*\n)+$)"), "");
    return s;
}

inline std::string remove_empty_lines(const std::string& input) {
    return std::regex_replace(input, std::regex(R"((^\s*\n)+)"), "");
}

bool is_thinking_model(const std::string& model_name) {
    return str_contains(model_name, "Qwen3") || str_contains(model_name, "DeepSeek");
}

std::string remove_tool_call_content(const std::string& input) {
    std::regex tool_call_block(R"(<tool_call>[\s\S]*?<\/tool_call>\s*)");
    std::regex name_line(R"((\s*\{ *"name": [^\n]*\n?))");
    std::string result = std::regex_replace(input, tool_call_block, "");
    result = std::regex_replace(result, name_line, "");
    result = remove_empty_lines(result);

    return result;
}

std::string escape_string(const std::string& input) {
    static const std::unordered_map<char, std::string> escape_map = {
        {'"', "\\\""}, {'\\', "\\\\"}, {'\n', "\\n"}, {'\r', "\\r"}, {'\t', "\\t"}, {'\b', "\\b"}, {'\f', "\\f"}
    };

    std::string result;
    result.reserve(input.size());

    for (char c : input) {
        auto it = escape_map.find(c);
        if (it != escape_map.end()) {
            result += it->second;
        } else {
            result += c;
        }
    }

    return result;
}

bool str_search(const std::string& source, const std::string& target) {
    auto it = std::search(
        source.begin(), source.end(),
        target.begin(), target.end(),
        [](unsigned char ch1, unsigned char ch2) {
            return std::tolower(ch1) == std::tolower(ch2);
        }
    );
    return it != source.end();
}

std::string extract_tag(const std::string& line, const std::string& tag) {
    size_t tagPos = line.find(tag);
    if (tagPos != std::string::npos) {
        return line.substr(tagPos + tag.length());
    }
    return "";
}

std::string str_replace(const std::string& str, const std::string& from, const std::string& to) {
    std::string result = str;
    size_t pos = 0;
    while ((pos = result.find(from, pos)) != std::string::npos) {
        result.replace(pos, from.length(), to);
        pos += to.length();
    }
    return result;
}

std::string generate_uuid4() {
    static std::random_device rd;
    static std::mt19937 gen(rd());
    static std::uniform_int_distribution<> dis(0, 15);
    static std::uniform_int_distribution<> dis2(8, 11);

    auto generate_hex = [&](int count) {
        std::stringstream ss;
        for (int i = 0; i < count; ++i) {
            ss << std::hex << dis(gen);
        }
        return ss.str();
    };

    std::stringstream ss;
    ss << generate_hex(8) << "-"      // 8 hex digits
       << generate_hex(4) << "-"      // 4 hex digits
       << "4" << generate_hex(3) << "-" // 4 + 3 hex digits (UUID version 4)
       << dis2(gen) << generate_hex(3) << "-" // 1 special + 3 hex digits
       << generate_hex(12);           // 12 hex digits

    return "chatcmpl-" + ss.str();
}

/////////////////////////////////////////////////////////////////////////////
/// Class TimerHelper declaration.
/////////////////////////////////////////////////////////////////////////////
#ifdef _WIN32
#pragma warning(disable:4251)
#endif
class TimerHelper
{
public:
    TimerHelper() {
        Reset();
    }

    void Reset() {
        time_start = std::chrono::steady_clock::now();
    }

    void Print(std::string message) {
        time_now = std::chrono::steady_clock::now();
        double dr_ms = std::chrono::duration<double, std::milli>(time_now - time_start).count();
        // printf("Time: %s %.2f ms\n", message.c_str(), dr_ms);
        std::cout << YELLOW << "INFO: [TIME] | " << message << " " << std::fixed << std::setprecision(2) << dr_ms << " ms" << RESET << std::endl;
    }

    void Print(std::string message, bool reset) {
        Print(message);
        if (reset) {
            Reset();
        }
    }

    long long GetSystemTime() {
        auto now = std::chrono::system_clock::now();
        auto duration = now.time_since_epoch();
        auto milliseconds = std::chrono::duration_cast<std::chrono::milliseconds>(duration).count();

        return milliseconds;
    }

private:
    std::chrono::steady_clock::time_point time_start;
    std::chrono::steady_clock::time_point time_now;
};


/////////////////////////////////////////////////////////////////////////////
/// Class RecursiveCharacterTextSplitter declaration.
/////////////////////////////////////////////////////////////////////////////

// When writing the C++ implementation of RecursiveCharacterTextSplitter, I referenced the Python code in LangChain.
// https://github.com/langchain-ai/langchain/blob/master/libs/text-splitters/langchain_text_splitters/character.py#L58
class RecursiveCharacterTextSplitter {
private:
    std::vector<std::string> _separators;
    bool _keep_separator;
    size_t _chunk_size;
    std::function<size_t(const std::string&)> _length_function;

    std::vector<std::string> _merge_splits(const std::vector<std::string>& splits, const std::string& separator) {
      std::vector<std::string> docs;
      std::string current_doc;
      int total = 0;

      for (const auto& split : splits) {
        size_t len = _length_function(split);
          if (total + len > _chunk_size) {
              if (!current_doc.empty()) {
                  docs.push_back(current_doc);
                  current_doc.clear();
              }
              total = 0;
          }
          if (!current_doc.empty()) current_doc += separator;
          current_doc += split;
          total += len;
      }
      if (!current_doc.empty()) docs.push_back(current_doc);

      return docs;
    }

    std::vector<std::string> _split_text(const std::string& text, const std::vector<std::string>& separators) {
      if (separators.empty()) return {text};

      std::string separator = separators.front();
      std::vector<std::string> splits;
      std::regex re(std::regex_replace(separator, std::regex(R"([\.\^\$\*\+\-\?\(\)\[\]\{\}\|\\])"), R"(\$&)"));
      std::sregex_token_iterator iter(text.begin(), text.end(), re, _keep_separator ? -1 : 0);
      std::sregex_token_iterator end;

      for (; iter != end; ++iter) {
          splits.push_back(*iter);
      }

      std::vector<std::string> final_chunks;
      for (const auto& split : splits) {
          if (_length_function(split) < _chunk_size) {
              final_chunks.push_back(split);
          } else {
              auto deeper_chunks = _split_text(split, {separators.begin() + 1, separators.end()});
              final_chunks.insert(final_chunks.end(), deeper_chunks.begin(), deeper_chunks.end());
          }
      }

      return _merge_splits(final_chunks, _keep_separator ? separator : "");
    }

public:
    RecursiveCharacterTextSplitter(
        const std::vector<std::string>& separators = {"\n\n", "\n", " ", ""},
        bool keep_separator = true,
        int chunk_size = DOCS_MAX_SIZE,
        std::function<size_t(const std::string&)> length_function = [](const std::string& s) { return static_cast<size_t>(s.length()); }
    ) : _separators(separators), _keep_separator(keep_separator),
        _chunk_size(chunk_size), _length_function(length_function){}

    std::vector<std::string> split_text(const std::string& text) {
        return _split_text(text, _separators);
    }
};

#endif
