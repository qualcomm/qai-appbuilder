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

#include <regex>
#include <nlohmann/json.hpp>
#include <core/log.h>

using json = nlohmann::ordered_json;

#ifdef __ANDROID__
inline int __argc;
inline char **__argv;
#endif

extern std::atomic<bool> http_busy_;
const int CONTEXT_SIZE = 4096;
const int DOCS_MAX_SIZE = CONTEXT_SIZE - 1024;
const int DOCS_MAX_QUERY_TIMES = 3;

const std::string MIMETYPE_JSON = "application/json; charset=utf-8";

struct File
{
    static bool IsFileExist(const std::string &);

    static bool IsFileEmpty(const std::string &);

    static size_t get_file_size(const std::string &);

    static std::unique_ptr<int8_t[]> get_file_as_buffer(const std::string &, uint32_t &size);
};

template<typename F, typename... Args>
double MeasureSeconds(F &&fn, Args &&... args)
{
    using clock = std::chrono::steady_clock;
    auto t0 = clock::now();
    std::forward<F>(fn)(std::forward<Args>(args)...);
    auto t1 = clock::now();
    std::chrono::duration<double> elapsed = t1 - t0;
    return elapsed.count();
}

extern class TimerHelper
{
public:
    TimerHelper();

    void Reset();

    void Print(const std::string &message);

    void Print(std::string message, bool reset);

    long long GetSystemTime();

private:
    class Impl;

    Impl *impl_;
} timer;


bool isPortAvailable(int port);

inline bool str_contains(const std::string &str, const std::string &sub)
{
    if (str.length() < sub.length())
        return false;
    std::string str_lower = str;
    std::string sub_lower = sub;
    std::transform(str_lower.begin(), str_lower.end(), str_lower.begin(), ::tolower);
    std::transform(sub_lower.begin(), sub_lower.end(), sub_lower.begin(), ::tolower);
    return str_lower.find(sub_lower) != std::string::npos;
}

inline std::string str_replace(const std::string &str, const std::string &from, const std::string &to)
{
    std::string result = str;
    size_t pos = 0;
    while ((pos = result.find(from, pos)) != std::string::npos)
    {
        result.replace(pos, from.length(), to);
        pos += to.length();
    }
    return result;
}

template<typename T>
T get_json_value(const json &jsonData, const std::string &key, const T &defaultValue)
{
    try
    {
        if (jsonData.contains(key))
        {
            return jsonData[key].get<T>();
        }
    }
    catch (const std::exception &e)
    {
        // My_Log{} << "Error getting JSON value for key '" << key << "': " << e.what() << ". Try to dump string." << std::endl;
        try
        {
            if constexpr (std::is_same<T, std::string>::value)
            {
                std::string texts = "";

                if (jsonData[key].is_array())
                {
                    for (const auto &item: jsonData[key])
                    {
                        if (item.is_string())
                        {
                            texts += item.get<std::string>();
                        }
                        else
                        {
                            for (auto it = item.begin(); it != item.end(); ++it)
                            {
                                if (it.value().is_string())
                                {
                                    if (!texts.empty())
                                    {
                                        texts += " ";
                                    }
                                    texts += it.value().get<std::string>();
                                }
                            }
                        }
                    }
                }

                return texts;
            }
        }
        catch (const std::exception &e)
        {
            My_Log{} << "Error getting JSON value for key '" << key << "': " << e.what() << std::endl;
        }
    }

    return defaultValue;
}

inline std::string json_to_str(const json &data)
{
    return data.dump(-1, ' ', false, json::error_handler_t::replace);
}

inline bool hasInvalidUtf8Chars(const std::string &str)
{
    const uint8_t *data = reinterpret_cast<const uint8_t *>(str.data());
    size_t length = str.size();
    size_t i = 0;

    while (i < length)
    {
        uint8_t byte = data[i];

        if ((byte & 0x80) == 0)
        {
            i++;
            continue;
        }

        int numBytes;
        if ((byte & 0xE0) == 0xC0)
        {
            numBytes = 1;
        }
        else if ((byte & 0xF0) == 0xE0)
        {
            numBytes = 2;
        }
        else if ((byte & 0xF8) == 0xF0)
        {
            numBytes = 3;
        }
        else
        {
            return true;
        }

        if (i + numBytes >= length)
        {
            return true;
        }

        for (int j = 1; j <= numBytes; j++)
        {
            if ((data[i + j] & 0xC0) != 0x80)
            {
                return true;
            }
        }

        i += numBytes + 1;
    }

    return false;
}

inline bool starts_with(const std::string &str, const std::string &prefix)
{
    return str.rfind(prefix, 0) == 0;
}


inline std::string trim_empty_lines(const std::string &input)
{
    std::string s = input;
    s = std::regex_replace(s, std::regex(R"(^(\s*\n)+)"), "");
    s = std::regex_replace(s, std::regex(R"((\s*\n)+$)"), "");
    return s;
}

inline bool is_thinking_model(const std::string &model_name)
{
    return str_contains(model_name, "Qwen3") ||
           str_contains(model_name, "DeepSeek") ||
           str_contains(model_name, "Hunyuan");
}

inline std::string escape_string(const std::string &input)
{
    static const std::unordered_map<char, std::string> escape_map = {
            {'"',  "\\\""},
            {'\\', "\\\\"},
            {'\n', "\\n"},
            {'\r', "\\r"},
            {'\t', "\\t"},
            {'\b', "\\b"},
            {'\f', "\\f"}
    };

    std::string result;
    result.reserve(input.size());

    for (char c: input)
    {
        auto it = escape_map.find(c);
        if (it != escape_map.end())
        {
            result += it->second;
        }
        else
        {
            result += c;
        }
    }

    return result;
}

inline bool str_search(const std::string &source, const std::string &target)
{
    auto it = std::search(
            source.begin(), source.end(),
            target.begin(), target.end(),
            [](unsigned char ch1, unsigned char ch2)
            {
                return std::tolower(ch1) == std::tolower(ch2);
            }
    );
    return it != source.end();
}

inline std::string extract_tag(const std::string &line, const std::string &tag)
{
    size_t tagPos = line.find(tag);
    if (tagPos != std::string::npos)
    {
        return line.substr(tagPos + tag.length());
    }
    return "";
}



/////////////////////////////////////////////////////////////////////////////
/// Class TimerHelper declaration.
/////////////////////////////////////////////////////////////////////////////
#ifdef _WIN32
#pragma warning(disable:4251)
#endif

/////////////////////////////////////////////////////////////////////////////
/// Class RecursiveCharacterTextSplitter declaration.
/////////////////////////////////////////////////////////////////////////////

// When writing the C++ implementation of RecursiveCharacterTextSplitter, I referenced the Python code in LangChain.
// https://github.com/langchain-ai/langchain/blob/master/libs/text-splitters/langchain_text_splitters/character.py#L58
class RecursiveCharacterTextSplitter
{
private:
    std::vector<std::string> _separators;
    bool _keep_separator;
    size_t _chunk_size;
    std::function<size_t(const std::string &)> _length_function;

    std::vector<std::string> _merge_splits(const std::vector<std::string> &splits, const std::string &separator)
    {
        std::vector<std::string> docs;
        std::string current_doc;
        int total = 0;

        for (const auto &split: splits)
        {
            size_t len = _length_function(split);
            if (total + len > _chunk_size)
            {
                if (!current_doc.empty())
                {
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

    std::vector<std::string> _split_text(const std::string &text, const std::vector<std::string> &separators)
    {
        if (separators.empty()) return {text};

        std::string separator = separators.front();
        std::vector<std::string> splits;
        std::regex re(std::regex_replace(separator, std::regex(R"([\.\^\$\*\+\-\?\(\)\[\]\{\}\|\\])"), R"(\$&)"));
        std::sregex_token_iterator iter(text.begin(), text.end(), re, _keep_separator ? -1 : 0);
        std::sregex_token_iterator end;

        for (; iter != end; ++iter)
        {
            splits.push_back(*iter);
        }

        std::vector<std::string> final_chunks;
        for (const auto &split: splits)
        {
            if (_length_function(split) < _chunk_size)
            {
                final_chunks.push_back(split);
            }
            else
            {
                auto deeper_chunks = _split_text(split, {separators.begin() + 1, separators.end()});
                final_chunks.insert(final_chunks.end(), deeper_chunks.begin(), deeper_chunks.end());
            }
        }

        return _merge_splits(final_chunks, _keep_separator ? separator : "");
    }

public:
    RecursiveCharacterTextSplitter(
            const std::vector<std::string> &separators = {"\n\n", "\n", " ", ""},
            bool keep_separator = true,
            int chunk_size = DOCS_MAX_SIZE,
            std::function<size_t(const std::string &)> length_function = [](const std::string &s)
            { return static_cast<size_t>(s.length()); }
    ) : _separators(separators), _keep_separator(keep_separator),
        _chunk_size(chunk_size), _length_function(length_function)
    {}

    std::vector<std::string> split_text(const std::string &text)
    {
        return _split_text(text, _separators);
    }
};

#endif
