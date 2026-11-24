//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef RETHINK_GATE_H
#define RETHINK_GATE_H

#include <algorithm>
#include <cctype>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace RethinkGate
{
enum class Decision
{
    Pending,
    Rethink,
    Skip
};

inline Decision Decide(const std::string &buffer, const std::vector<std::string> &tools)
{
    const auto marker = buffer.find("<tool_call>");
    if (marker == std::string::npos)
        return Decision::Pending;

    std::string prefix = buffer.substr(0, marker);
    size_t cursor = 0;
    while ((cursor = prefix.find("<think>", cursor)) != std::string::npos)
    {
        const auto close = prefix.find("</think>", cursor + 7);
        if (close == std::string::npos)
            return Decision::Skip;
        prefix.erase(cursor, close + 8 - cursor);
    }
    if (!std::all_of(prefix.begin(), prefix.end(), [](unsigned char ch) { return std::isspace(ch) != 0; }))
        return Decision::Skip;

    size_t name_pos = buffer.find("\"name\"", marker + 11);
    if (name_pos == std::string::npos)
        return Decision::Pending;
    size_t value_pos = buffer.find(':', name_pos + 6);
    if (value_pos == std::string::npos)
        return Decision::Pending;
    ++value_pos;
    while (value_pos < buffer.size() && std::isspace(static_cast<unsigned char>(buffer[value_pos])))
        ++value_pos;
    if (value_pos == buffer.size())
        return Decision::Pending;
    if (buffer[value_pos] != '\"')
        return Decision::Skip;

    size_t end = value_pos + 1;
    bool escaped = false;
    for (; end < buffer.size(); ++end)
    {
        const char ch = buffer[end];
        if (escaped)
        {
            escaped = false;
            continue;
        }
        if (ch == '\\')
        {
            escaped = true;
            continue;
        }
        if (ch == '\"')
            break;
    }
    if (end == buffer.size())
        return Decision::Pending;

    std::string name;
    try
    {
        name = nlohmann::json::parse(buffer.substr(value_pos, end - value_pos + 1)).get<std::string>();
    }
    catch (...)
    {
        return Decision::Skip;
    }
    return std::find(tools.begin(), tools.end(), name) != tools.end() ? Decision::Rethink : Decision::Skip;
}
}

#endif
