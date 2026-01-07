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
#include "log.h"

using json = nlohmann::ordered_json;

inline std::string CurrentDir;
inline std::string RootDir;

extern std::atomic<bool> http_busy_;
const std::string MIMETYPE_JSON = "application/json; charset=utf-8";

struct JsonError : public std::exception
{
public:
    JsonError(std::string &&msg) : msg_{std::move(msg)}
    {}

    const char *what() const noexcept final
    {
        return msg_.c_str();
    }

private:
    std::string msg_;
};

struct File
{
    static bool IsFileExist(const std::string &);

    static bool IsFileEmpty(const std::string &);

    static size_t get_file_size(const std::string &, std::ios::openmode);

    static std::unique_ptr<int8_t[]> get_file_as_buffer(const std::string &, uint32_t &size);

    static bool MatchFileInDir(const std::string &dir_path, const std::string &file, bool is_ext);

    static std::vector<std::string> SearchExtInDir(const std::string &dir_path, const std::string &ext);

    static std::vector<uint8_t> ReadFile(const std::string &file_name, bool binary = true);
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

inline class Timer
{
public:
    Timer();

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
            throw std::runtime_error("getting json value for key: " + key + " ," + e.what());
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

#endif
