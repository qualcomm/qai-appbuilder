//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef TASK_MEMO_DOC_SCAN_H
#define TASK_MEMO_DOC_SCAN_H

#include <nlohmann/json.hpp>
#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <utility>
#include <vector>

namespace task_memo_scan
{
using json = nlohmann::ordered_json;

constexpr size_t kMaxHeadsPerFile = 32;
constexpr size_t kMaxIdentsPerFile = 64;
constexpr size_t kHeadTitleChars = 60;
constexpr size_t kSectionsRenderChars = 480;

struct Heading
{
    size_t line = 0;
    std::string title;
};

struct DocRef
{
    std::string path;
    std::string section;
    std::string cite;
};

struct PageText
{
    std::string text;
    size_t first = 1;
};

inline size_t Utf8Floor(const std::string &s, size_t pos)
{
    if (pos >= s.size())
        return s.size();
    while (pos > 0 && (static_cast<unsigned char>(s[pos]) & 0xC0) == 0x80)
        --pos;
    return pos;
}

inline std::string Trim(const std::string &s)
{
    const size_t b = s.find_first_not_of(" \t\r");
    if (b == std::string::npos)
        return "";
    const size_t e = s.find_last_not_of(" \t\r");
    return s.substr(b, e - b + 1);
}

inline std::string ClipHead(const std::string &s, size_t cap)
{
    if (s.size() <= cap)
        return s;
    return s.substr(0, Utf8Floor(s, cap)) + "...";
}

inline std::string ClipKeepTail(const std::string &s, size_t cap)
{
    if (s.size() <= cap)
        return s;
    if (cap < 40)
        return ClipHead(s, cap);
    const size_t tail = cap / 3;
    const size_t head = cap - tail - 5;
    size_t tail_begin = s.size() - tail;
    while (tail_begin < s.size() && (static_cast<unsigned char>(s[tail_begin]) & 0xC0) == 0x80)
        ++tail_begin;
    return s.substr(0, Utf8Floor(s, head)) + " ... " + s.substr(tail_begin);
}

inline std::string Lower(std::string s)
{
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

inline std::vector<std::string> SplitLines(const std::string &text)
{
    std::vector<std::string> lines;
    size_t pos = 0;
    while (pos <= text.size())
    {
        const size_t nl = text.find('\n', pos);
        if (nl == std::string::npos)
        {
            lines.push_back(text.substr(pos));
            break;
        }
        lines.push_back(text.substr(pos, nl - pos));
        pos = nl + 1;
    }
    return lines;
}

inline bool IsFenceLine(const std::string &line)
{
    const std::string t = Trim(line);
    return t.compare(0, 3, "```") == 0 || t.compare(0, 3, "~~~") == 0;
}

inline bool ParsePagedReadFooter(const std::string &content, size_t &first, size_t &last, size_t &total)
{
    const size_t marker = content.rfind("showed lines ");
    return marker != std::string::npos &&
           std::sscanf(content.c_str() + marker, "showed lines %zu-%zu of total %zu", &first, &last, &total) == 3;
}

inline bool HasPagedReadFooter(const std::string &content)
{
    size_t first = 0, last = 0, total = 0;
    return ParsePagedReadFooter(content, first, last, total);
}

inline PageText ExtractPageText(const std::string &content, size_t first_line_hint)
{
    PageText page;
    page.first = first_line_hint;
    size_t begin = 0;
    static const char kWrapper[] = "<tool_response>\n";
    if (content.compare(0, sizeof(kWrapper) - 1, kWrapper) == 0)
        begin = sizeof(kWrapper) - 1;
    size_t end = content.size();
    size_t footer_first = 0, footer_last = 0, footer_total = 0;
    if (ParsePagedReadFooter(content, footer_first, footer_last, footer_total))
    {
        page.first = footer_first;
        const size_t marker = content.rfind("showed lines ");
        const size_t line_start = content.rfind('\n', marker);
        end = (line_start == std::string::npos) ? 0 : line_start + 1;
    }
    else
    {
        const size_t snapshot = content.find("\n[snapshot:", begin);
        if (snapshot != std::string::npos)
            end = snapshot + 1;
    }
    if (end > begin)
        page.text = content.substr(begin, end - begin);
    while (!page.text.empty() && (page.text.back() == '\n' || page.text.back() == '\r'))
        page.text.pop_back();
    return page;
}

inline bool IsHardRuleLine(const std::string &line)
{
    static const char *kMarkers[] = {"NEVER", "MUST", "MANDATORY", "Do NOT", "DO NOT", "必须", "禁止", "不得", "严禁"};
    for (const char *k : kMarkers)
        if (line.find(k) != std::string::npos)
            return true;
    return false;
}

inline std::vector<std::string> ExtractHardRules(const std::string &content, size_t max_rules, size_t cap)
{
    std::vector<std::string> out;
    for (const std::string &raw : SplitLines(content))
    {
        if (out.size() >= max_rules)
            break;
        const size_t first = raw.find_first_not_of(" \t\r>");
        if (first == std::string::npos || raw[first] == '#' || raw[first] == '|')
            continue;
        const size_t body = raw.find_first_not_of(" \t\r>-*", first);
        if (body == std::string::npos)
            continue;
        const std::string line = Trim(raw.substr(body));
        if (line.size() < 8 || !IsHardRuleLine(line))
            continue;
        out.push_back(ClipKeepTail(line, cap));
    }
    return out;
}

inline std::vector<Heading> ExtractHeadings(const std::string &text, size_t first_line)
{
    std::vector<Heading> out;
    bool in_fence = false;
    size_t index = 0;
    for (const std::string &line : SplitLines(text))
    {
        const size_t line_no = first_line + index++;
        if (IsFenceLine(line))
        {
            in_fence = !in_fence;
            continue;
        }
        if (in_fence)
            continue;
        size_t pos = 0;
        while (pos < line.size() && pos < 3 && line[pos] == ' ')
            ++pos;
        size_t hashes = 0;
        while (pos + hashes < line.size() && line[pos + hashes] == '#')
            ++hashes;
        if (hashes == 0 || hashes > 6 || pos + hashes >= line.size() || line[pos + hashes] != ' ')
            continue;
        std::string title = Trim(line.substr(pos + hashes));
        while (!title.empty() && (title.back() == '#' || title.back() == ' '))
            title.pop_back();
        if (title.empty())
            continue;
        out.push_back({line_no, ClipHead(title, kHeadTitleChars)});
    }
    return out;
}

inline bool IsPathChar(char c)
{
    const unsigned char u = static_cast<unsigned char>(c);
    return std::isalnum(u) || c == '_' || c == '.' || c == '/' || c == '\\' || c == ':' || c == '~' || c == '-';
}

inline std::string NormalizePath(const std::string &path)
{
    std::string p = Lower(path);
    std::replace(p.begin(), p.end(), '\\', '/');
    while (p.compare(0, 2, "./") == 0)
        p.erase(0, 2);
    return p;
}

inline bool EndsWithPath(const std::string &longer, const std::string &suffix)
{
    if (suffix.empty() || suffix.size() > longer.size())
        return false;
    if (longer.compare(longer.size() - suffix.size(), suffix.size(), suffix) != 0)
        return false;
    return suffix.size() == longer.size() || longer[longer.size() - suffix.size() - 1] == '/' || suffix[0] == '/';
}

inline bool PathMatches(const std::string &a, const std::string &b)
{
    const std::string na = NormalizePath(a);
    const std::string nb = NormalizePath(b);
    return EndsWithPath(na, nb) || EndsWithPath(nb, na);
}

inline std::vector<DocRef> ExtractDocRefs(const std::string &text, size_t max_refs, size_t cite_cap)
{
    std::vector<DocRef> out;
    bool in_fence = false;
    for (const std::string &line : SplitLines(text))
    {
        if (out.size() >= max_refs)
            break;
        if (IsFenceLine(line))
        {
            in_fence = !in_fence;
            continue;
        }
        const std::string trimmed = Trim(line);
        if (in_fence || trimmed.empty() || trimmed[0] == '|')
            continue;
        size_t pos = 0;
        while (out.size() < max_refs && (pos = line.find(".md", pos)) != std::string::npos)
        {
            const size_t end = pos + 3;
            const bool boundary = end >= line.size() || !std::isalnum(static_cast<unsigned char>(line[end]));
            size_t begin = pos;
            while (begin > 0 && IsPathChar(line[begin - 1]))
                --begin;
            pos = end;
            if (!boundary)
                continue;
            std::string path = line.substr(begin, end - begin);
            if (path.find("://") != std::string::npos)
                continue;
            for (size_t i = path.size(); i-- > 0;)
            {
                if (path[i] == ':' && !(i == 1 && std::isalpha(static_cast<unsigned char>(path[0]))))
                {
                    path.erase(0, i + 1);
                    break;
                }
            }
            if (path.find('/') == std::string::npos && path.find('\\') == std::string::npos)
                continue;
            std::replace(path.begin(), path.end(), '\\', '/');
            while (path.compare(0, 2, "./") == 0)
                path.erase(0, 2);
            std::string section;
            size_t rest = end;
            while (rest < line.size() && (line[rest] == ' ' || line[rest] == '`' || line[rest] == '*' ||
                                          line[rest] == ')' || line[rest] == ']' || line[rest] == '"'))
                ++rest;
            if (line.compare(rest, 2, "\xC2\xA7") == 0)
            {
                rest += 2;
                while (rest < line.size() && line[rest] == ' ')
                    ++rest;
                size_t stop = rest;
                while (stop < line.size() && line[stop] != '`' && line[stop] != ')' && line[stop] != '|' &&
                       line[stop] != ';' && line[stop] != '"')
                    ++stop;
                section = ClipHead(Trim(line.substr(rest, stop - rest)), kHeadTitleChars);
            }
            bool dup = false;
            for (const DocRef &r : out)
                dup = dup || (r.path == path && r.section == section);
            if (dup)
                continue;
            const size_t body = trimmed.find_first_not_of(">-* \t");
            out.push_back({path, section, ClipKeepTail(trimmed.substr(body == std::string::npos ? 0 : body), cite_cap)});
        }
    }
    return out;
}

inline bool HasScriptExtension(const std::string &lower)
{
    static const char *kExts[] = {".py", ".sh", ".bat", ".ps1", ".exe", ".cmd", ".js"};
    for (const char *e : kExts)
    {
        const size_t n = std::char_traits<char>::length(e);
        if (lower.size() > n && lower.compare(lower.size() - n, n, e) == 0)
            return true;
    }
    return false;
}

inline std::string FirstCommandWord(const std::string &line)
{
    const std::string t = Trim(line);
    if (t.empty() || t[0] == '"' || t[0] == '{' || t[0] == '[' || t[0] == '}' || t[0] == '#' || t[0] == '/' ||
        t[0] == '<')
        return "";
    size_t i = 0;
    while (i < t.size() && (std::isalnum(static_cast<unsigned char>(t[i])) || t[i] == '_' || t[i] == '.' || t[i] == '-'))
        ++i;
    if (i < 3 || (i < t.size() && t[i] == ':'))
        return "";
    const std::string word = Lower(t.substr(0, i));
    if (!std::isalpha(static_cast<unsigned char>(word[0])) || word.find_first_of("-_.") == std::string::npos)
        return "";
    return word;
}

inline std::vector<std::string> SplitNameTokens(const std::string &line)
{
    std::vector<std::string> out;
    size_t i = 0;
    while (i < line.size())
    {
        while (i < line.size() && !(std::isalnum(static_cast<unsigned char>(line[i])) || line[i] == '_' ||
                                    line[i] == '.' || line[i] == '-'))
            ++i;
        const size_t b = i;
        while (i < line.size() && (std::isalnum(static_cast<unsigned char>(line[i])) || line[i] == '_' ||
                                   line[i] == '.' || line[i] == '-'))
            ++i;
        std::string tok = line.substr(b, i - b);
        while (!tok.empty() && (tok.back() == '.' || tok.back() == '-'))
            tok.pop_back();
        if (tok.size() >= 3)
            out.push_back(Lower(tok));
    }
    return out;
}

inline std::vector<std::pair<std::string, size_t>> ExtractIdentifiers(const std::string &text, size_t first_line,
                                                                      size_t max_n)
{
    std::vector<std::pair<std::string, size_t>> out;
    auto add = [&](const std::string &tok, size_t line_no) {
        if (out.size() >= max_n || tok.size() > 48)
            return;
        for (const auto &o : out)
            if (o.first == tok)
                return;
        out.emplace_back(tok, line_no);
    };
    bool in_fence = false;
    size_t index = 0;
    for (const std::string &line : SplitLines(text))
    {
        const size_t line_no = first_line + index++;
        if (IsFenceLine(line))
        {
            in_fence = !in_fence;
            continue;
        }
        for (const std::string &tok : SplitNameTokens(line))
            if (HasScriptExtension(tok))
                add(tok, line_no);
        if (in_fence)
        {
            const std::string command_word = FirstCommandWord(line);
            if (!command_word.empty())
                add(command_word, line_no);
        }
    }
    return out;
}

inline int FindIdentLine(const json &idents, const std::vector<std::string> &call_tokens)
{
    if (!idents.is_object())
        return 0;
    for (const std::string &tok : call_tokens)
    {
        auto it = idents.find(tok);
        if (it != idents.end() && it->is_number_integer())
            return it->get<int>();
    }
    return 0;
}

inline void FoldPage(json &file_entry, const std::string &text, size_t first_line)
{
    if (!file_entry.is_object())
        file_entry = json::object();
    std::vector<Heading> heads;
    if (file_entry.contains("heads") && file_entry["heads"].is_array())
        for (const auto &h : file_entry["heads"])
            if (h.is_array() && h.size() == 2 && h[0].is_number_integer() && h[1].is_string())
                heads.push_back({h[0].get<size_t>(), h[1].get<std::string>()});
    for (const Heading &h : ExtractHeadings(text, first_line))
    {
        bool known = false;
        for (const Heading &e : heads)
            known = known || e.line == h.line;
        if (!known)
            heads.push_back(h);
    }
    std::sort(heads.begin(), heads.end(), [](const Heading &a, const Heading &b) { return a.line < b.line; });
    while (heads.size() > kMaxHeadsPerFile)
        heads.erase(heads.begin());
    if (!heads.empty())
    {
        json arr = json::array();
        for (const Heading &h : heads)
            arr.push_back(json::array({h.line, h.title}));
        file_entry["heads"] = arr;
    }
    if (!file_entry.contains("idents") || !file_entry["idents"].is_object())
        file_entry["idents"] = json::object();
    for (const auto &id : ExtractIdentifiers(text, first_line, kMaxIdentsPerFile))
    {
        if (file_entry["idents"].size() >= kMaxIdentsPerFile)
            break;
        if (!file_entry["idents"].contains(id.first))
            file_entry["idents"][id.first] = id.second;
    }
}

inline void FoldRefs(json &pending, const std::vector<DocRef> &refs, const json &read_files,
                     const std::string &own_path, size_t cap)
{
    if (!pending.is_object())
        pending = json::object();
    for (const DocRef &ref : refs)
    {
        if (PathMatches(ref.path, own_path) || pending.contains(ref.path))
            continue;
        bool already_read = false;
        if (read_files.is_object())
            for (auto it = read_files.begin(); it != read_files.end() && !already_read; ++it)
                already_read = PathMatches(ref.path, it.key());
        if (already_read)
            continue;
        while (pending.size() >= cap)
            pending.erase(pending.begin());
        pending[ref.path] = json{{"section", ref.section}, {"cite", ref.cite}};
    }
}

inline void ClearReadRef(json &pending, const std::string &read_path)
{
    if (!pending.is_object())
        return;
    std::vector<std::string> gone;
    for (auto it = pending.begin(); it != pending.end(); ++it)
        if (PathMatches(it.key(), read_path))
            gone.push_back(it.key());
    for (const std::string &k : gone)
        pending.erase(k);
}

inline std::string LastNameOf(const std::string &path)
{
    const std::string p = NormalizePath(path);
    const size_t slash = p.rfind('/');
    return slash == std::string::npos ? p : p.substr(slash + 1);
}

inline std::vector<Heading> LoadHeadings(const json &file_entry)
{
    std::vector<Heading> out;
    if (file_entry.is_object() && file_entry.contains("heads") && file_entry["heads"].is_array())
        for (const auto &h : file_entry["heads"])
            if (h.is_array() && h.size() == 2 && h[0].is_number_integer() && h[1].is_string())
                out.push_back({h[0].get<size_t>(), h[1].get<std::string>()});
    return out;
}

inline std::string RenderSectionsSuffix(const json &file_entry)
{
    const std::vector<Heading> heads = LoadHeadings(file_entry);
    if (heads.empty())
        return "";
    std::string out = "; sections seen: ";
    size_t used = 0;
    bool first = true;
    for (const Heading &h : heads)
    {
        const std::string item = "L" + std::to_string(h.line) + " " + h.title;
        if (used + item.size() > kSectionsRenderChars)
        {
            out += " ...";
            break;
        }
        if (!first)
            out += "; ";
        out += item;
        used += item.size() + 2;
        first = false;
    }
    return out;
}

inline std::string RenderCursorSuffix(const json &file_entry)
{
    if (!file_entry.is_object() || !file_entry.contains("used_line") || !file_entry["used_line"].is_number_integer())
        return "";
    const size_t used = file_entry["used_line"].get<size_t>();
    const std::vector<Heading> heads = LoadHeadings(file_entry);
    std::string out = "; an action was last matched to line " + std::to_string(used);
    const Heading *under = nullptr;
    const Heading *next = nullptr;
    for (const Heading &h : heads)
    {
        if (h.line <= used)
            under = &h;
        else if (!next)
            next = &h;
    }
    if (under)
        out += " (under \"" + under->title + "\")";
    if (next)
        out += ", next heading L" + std::to_string(next->line) + " " + next->title;
    return out;
}

inline std::string RenderPendingRefs(const json &pending)
{
    if (!pending.is_object() || pending.empty())
        return "";
    std::string out =
        "Documents named in what you read that have not been opened yet (state as of the last compaction; "
        "open one when its step is reached):\n";
    for (auto it = pending.begin(); it != pending.end(); ++it)
    {
        if (!it.value().is_object())
            continue;
        const std::string section = it.value().value("section", std::string());
        const std::string cite = it.value().value("cite", std::string());
        out += "- " + ClipKeepTail(it.key(), 120) + (section.empty() ? "" : " § " + section);
        if (!cite.empty())
            out += " -- \"" + cite + "\"";
        out += "\n";
    }
    return out;
}

inline bool IsWriteLikeRow(const std::string &row)
{
    const size_t colon = row.find(": ");
    if (colon == std::string::npos)
        return false;
    const size_t begin = colon + 2;
    const size_t end = row.find(' ', begin);
    const std::string tool = Lower(row.substr(begin, end == std::string::npos ? std::string::npos : end - begin));
    return tool.find("write") != std::string::npos || tool.find("edit") != std::string::npos;
}

inline void AppendCompletedRow(json &field, const std::string &row, size_t cap)
{
    if (!field.is_array())
        field = json::array();
    for (const auto &existing : field)
        if (existing.is_string() && existing.get<std::string>() == row)
            return;
    field.push_back(row);
    while (field.size() > cap)
    {
        size_t victim = 0;
        for (size_t i = 0; i + 1 < field.size(); ++i)
        {
            if (!field[i].is_string() || !IsWriteLikeRow(field[i].get<std::string>()))
            {
                victim = i;
                break;
            }
        }
        field.erase(field.begin() + static_cast<long long>(victim));
    }
}

}

#endif
