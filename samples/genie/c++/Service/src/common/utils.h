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

#include <nlohmann/json.hpp>
#include <functional>
#include <chrono>
#include <algorithm>
#include <cctype>

using json = nlohmann::ordered_json;

inline std::string CurrentDir;
inline std::string RootDir;

struct ReportError : public std::exception
{
    ReportError(std::string &&msg) : msg_{std::move(msg)} {}

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

    static bool MatchFileInDir(const std::string &dir_path,
                               const std::string &part,
                               std::vector<std::string> *files = nullptr);

    static std::string ResolveModelConfigPath(const std::string &model_dir);

    static std::string DeriveAncestorDir(const std::string &file_path,
                                         const std::string &base_dir,
                                         int levels);

    template<typename T>
    static std::vector<T> ReadFile(const std::string &file_name, bool binary = true);

    template<typename T>
    static void WriteBinaryFile(const std::vector<T> &buffer, const std::string &file_name)
    {
        WriteBinaryFile(buffer.data(), buffer.size() , file_name);
    }

    template<typename T>
    static void WriteBinaryFile(const T *buf, int size, const std::string &file_name);
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
T get_json_value(const json &jsonData, const std::string &key, const T &defaultValue);

inline std::string json_to_str(const json &data)
{
    return data.dump(-1, ' ', false, json::error_handler_t::replace);
}

inline bool starts_with(const std::string &str, const std::string &prefix)
{
    return str.rfind(prefix, 0) == 0;
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

inline bool sanitize_utf8_inplace(std::string& input)
{
    bool has_invalid = false;
    for (size_t i = 0; i < input.size(); )
    {
        unsigned char byte = static_cast<unsigned char>(input[i]);
        if ((byte & 0x80) == 0) {

            ++i;
        } else if ((byte & 0xE0) == 0xC0) {

            if (i + 1 < input.size() &&
                (static_cast<unsigned char>(input[i+1]) & 0xC0) == 0x80) {
                i += 2;
            } else {
                input[i] = '?';
                has_invalid = true;
                ++i;
            }
        } else if ((byte & 0xF0) == 0xE0) {

            if (i + 2 < input.size() &&
                (static_cast<unsigned char>(input[i+1]) & 0xC0) == 0x80 &&
                (static_cast<unsigned char>(input[i+2]) & 0xC0) == 0x80) {
                i += 3;
            } else {
                input[i] = '?';
                has_invalid = true;
                ++i;
            }
        } else if ((byte & 0xF8) == 0xF0) {

            if (i + 3 < input.size() &&
                (static_cast<unsigned char>(input[i+1]) & 0xC0) == 0x80 &&
                (static_cast<unsigned char>(input[i+2]) & 0xC0) == 0x80 &&
                (static_cast<unsigned char>(input[i+3]) & 0xC0) == 0x80) {
                i += 4;
            } else {
                input[i] = '?';
                has_invalid = true;
                ++i;
            }
        } else {

            input[i] = '?';
            has_invalid = true;
            ++i;
        }
    }
    return has_invalid;
}

inline void clean_control_characters_inplace(std::string& input)
{

    for (char& c : input)
    {
        unsigned char uc = static_cast<unsigned char>(c);

        if (uc < 0x20 && uc != '\n' && uc != '\r' && uc != '\t')
        {
            c = '?';
        }
    }

    sanitize_utf8_inplace(input);
}

class Utf8StreamProcessor
{
public:
    explicit Utf8StreamProcessor(std::function<void(std::string &)> callback)
            : callback(std::move(callback))
    {}

    void processStream(const char *str, size_t len)
    {
        utf8Buffer.append(str, len);

        size_t i = 0;
        std::string completeChars;
        while (i < utf8Buffer.size())
        {
            int length = utf8CharLength(static_cast<unsigned char>(utf8Buffer[i]));
            if (length == 0 || i + length > utf8Buffer.size())
            {
                break;
            }
            completeChars.append(utf8Buffer, i, length);
            i += length;
        }
        utf8Buffer = utf8Buffer.substr(i);
        if (!completeChars.empty())
        {
            callback(completeChars);
        }
    }

    static int utf8CharLength(unsigned char byte)
    {
        if ((byte & 0x80) == 0) return 1;
        if ((byte & 0xE0) == 0xC0) return 2;
        if ((byte & 0xF0) == 0xE0) return 3;
        if ((byte & 0xF8) == 0xF0) return 4;
        return 0;
    }

private:
    std::string utf8Buffer;
    std::function<void(std::string &)> callback;
};

inline size_t utf8_align_start(const std::string &str, size_t offset)
{
    const uint8_t *data = reinterpret_cast<const uint8_t *>(str.data());
    size_t len = str.size();

    while (offset < len && (data[offset] & 0xC0) == 0x80)
    {
        ++offset;
    }
    return offset;
}

inline std::string safe_utf8_truncate(const std::string &str, size_t max_bytes, const std::string &suffix = "...")
{
    if (str.length() <= max_bytes)
    {
        return str;
    }

    size_t target_length = max_bytes > suffix.length() ? max_bytes - suffix.length() : 0;
    if (target_length == 0)
    {
        return suffix.substr(0, max_bytes);
    }

    const uint8_t *data = reinterpret_cast<const uint8_t *>(str.data());
    size_t safe_length = 0;
    size_t i = 0;

    while (i < target_length && i < str.length())
    {
        uint8_t byte = data[i];

        int char_bytes = 1;
        if ((byte & 0x80) == 0)
        {

            char_bytes = 1;
        }
        else if ((byte & 0xE0) == 0xC0)
        {

            char_bytes = 2;
        }
        else if ((byte & 0xF0) == 0xE0)
        {

            char_bytes = 3;
        }
        else if ((byte & 0xF8) == 0xF0)
        {

            char_bytes = 4;
        }
        else
        {

            i++;
            continue;
        }

        if (i + char_bytes <= target_length)
        {
            safe_length = i + char_bytes;
            i += char_bytes;
        }
        else
        {

            break;
        }
    }

    return str.substr(0, safe_length) + suffix;
}

inline double EstimateCjkAwareBytesPerToken(const std::string &text,
                                            double cjk_bytes_per_token,
                                            double ascii_bytes_per_token)
{
    if (text.empty() || cjk_bytes_per_token <= 0.0 || ascii_bytes_per_token <= 0.0)
    {
        return (ascii_bytes_per_token > 0.0) ? ascii_bytes_per_token : 4.0;
    }

    const uint8_t *data = reinterpret_cast<const uint8_t *>(text.data());
    size_t len = text.size();
    size_t cjk_bytes = 0;
    size_t i = 0;
    while (i < len)
    {
        uint8_t byte = data[i];
        int char_bytes = 1;
        uint32_t code_point = byte;
        if ((byte & 0x80) == 0)
        {
            char_bytes = 1;
            code_point = byte;
        }
        else if ((byte & 0xE0) == 0xC0)
        {
            char_bytes = 2;
            code_point = byte & 0x1F;
        }
        else if ((byte & 0xF0) == 0xE0)
        {
            char_bytes = 3;
            code_point = byte & 0x0F;
        }
        else if ((byte & 0xF8) == 0xF0)
        {
            char_bytes = 4;
            code_point = byte & 0x07;
        }
        else
        {

            ++i;
            continue;
        }
        for (int k = 1; k < char_bytes && i + k < len; ++k)
        {
            code_point = (code_point << 6) | (data[i + k] & 0x3F);
        }

        if ((code_point >= 0x4E00 && code_point <= 0x9FFF) ||
            (code_point >= 0x3400 && code_point <= 0x4DBF) ||
            (code_point >= 0x3000 && code_point <= 0x303F) ||
            (code_point >= 0xFF00 && code_point <= 0xFFEF))
        {
            cjk_bytes += static_cast<size_t>(char_bytes);
        }
        i += static_cast<size_t>(char_bytes);
    }

    size_t other_bytes = (len > cjk_bytes) ? (len - cjk_bytes) : 0;
    double estimated_tokens = static_cast<double>(cjk_bytes) / cjk_bytes_per_token +
                              static_cast<double>(other_bytes) / ascii_bytes_per_token;
    if (estimated_tokens <= 0.0)
    {
        return ascii_bytes_per_token;
    }
    return static_cast<double>(len) / estimated_tokens;
}

#endif
