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

/**
 * Sanitize invalid UTF-8 byte sequences in-place.
 * Replaces any invalid UTF-8 byte with '?' to prevent Rust tokenizer
 * (GenieTokenizer_encode / GenieDialog_query) from panicking with Utf8Error.
 *
 * Returns true if any invalid bytes were found and replaced.
 */
inline bool sanitize_utf8_inplace(std::string& input)
{
    bool has_invalid = false;
    for (size_t i = 0; i < input.size(); )
    {
        unsigned char byte = static_cast<unsigned char>(input[i]);
        if ((byte & 0x80) == 0) {
            // ASCII single-byte character, valid
            ++i;
        } else if ((byte & 0xE0) == 0xC0) {
            // 2-byte sequence
            if (i + 1 < input.size() &&
                (static_cast<unsigned char>(input[i+1]) & 0xC0) == 0x80) {
                i += 2;
            } else {
                input[i] = '?';
                has_invalid = true;
                ++i;
            }
        } else if ((byte & 0xF0) == 0xE0) {
            // 3-byte sequence
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
            // 4-byte sequence
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
            // Invalid leading byte (continuation byte or out-of-range byte)
            input[i] = '?';
            has_invalid = true;
            ++i;
        }
    }
    return has_invalid;
}

inline void clean_control_characters_inplace(std::string& input)
{
    // Pass 1: Replace ASCII control characters (< 0x20) except whitespace
    for (char& c : input)
    {
        unsigned char uc = static_cast<unsigned char>(c);
        // Keep printable ASCII (>= 0x20), UTF-8 bytes (>= 0x80), and specific whitespace
        // Replace invalid control characters (like \0) with a safe placeholder
        if (uc < 0x20 && uc != '\n' && uc != '\r' && uc != '\t')
        {
            c = '?';
        }
    }

    // Pass 2: Fix invalid UTF-8 multi-byte sequences
    // This prevents Rust tokenizer (GenieTokenizer_encode / GenieDialog_query) from panicking
    // with "Utf8Error" when it receives invalid UTF-8 byte sequences.
    sanitize_utf8_inplace(input);
}

// Buffers raw text fed in incrementally (e.g. one LLM token at a time) and only forwards
// complete UTF-8 characters to the callback, holding back any trailing partial multi-byte
// sequence until the continuation bytes arrive. This is the correct fix for streaming output
// (as opposed to sanitize_utf8_inplace, which replaces bytes and would corrupt every
// multi-byte character that happens to be split across a token boundary).
// https://github.com/alibaba/MNN/blob/master/apps/Android/MnnLlmChat/app/src/main/cpp/utf8_stream_processor.hpp
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

/**
 * Advance a byte offset within a UTF-8 string forward until it points to the start of a valid
 * UTF-8 character (or the end of the string).  Use this to fix up an offset that was computed
 * by raw byte arithmetic and may therefore land in the middle of a multi-byte sequence.
 *
 * @param str    The UTF-8 string
 * @param offset Byte offset that may be inside a multi-byte character
 * @return       The smallest offset >= the input that is either the end of the string or the
 *               first byte of a valid UTF-8 character (i.e. not a continuation byte 10xxxxxx)
 */
inline size_t utf8_align_start(const std::string &str, size_t offset)
{
    const uint8_t *data = reinterpret_cast<const uint8_t *>(str.data());
    size_t len = str.size();
    // Continuation bytes have the pattern 10xxxxxx (0x80..0xBF).
    // Skip forward past any continuation bytes so we land on a leading byte.
    while (offset < len && (data[offset] & 0xC0) == 0x80)
    {
        ++offset;
    }
    return offset;
}

/**
 * Safely truncate a UTF-8 string to a maximum byte length without breaking multi-byte characters.
 * If the string is longer than max_bytes, it will be truncated and suffix will be appended.
 *
 * @param str The input UTF-8 string
 * @param max_bytes Maximum number of bytes (not characters) to keep
 * @param suffix Suffix to append when truncated (default: "...")
 * @return Truncated string with valid UTF-8 encoding
 */
inline std::string safe_utf8_truncate(const std::string &str, size_t max_bytes, const std::string &suffix = "...")
{
    if (str.length() <= max_bytes)
    {
        return str;
    }

    // Reserve space for suffix
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

        // Determine the number of bytes in this UTF-8 character
        int char_bytes = 1;
        if ((byte & 0x80) == 0)
        {
            // Single-byte character (0xxxxxxx)
            char_bytes = 1;
        }
        else if ((byte & 0xE0) == 0xC0)
        {
            // Two-byte character (110xxxxx)
            char_bytes = 2;
        }
        else if ((byte & 0xF0) == 0xE0)
        {
            // Three-byte character (1110xxxx)
            char_bytes = 3;
        }
        else if ((byte & 0xF8) == 0xF0)
        {
            // Four-byte character (11110xxx)
            char_bytes = 4;
        }
        else
        {
            // Invalid UTF-8 start byte, skip it
            i++;
            continue;
        }

        // Check if the complete character fits within the target length
        if (i + char_bytes <= target_length)
        {
            safe_length = i + char_bytes;
            i += char_bytes;
        }
        else
        {
            // The character would be cut off, stop here
            break;
        }
    }

    return str.substr(0, safe_length) + suffix;
}

#endif
