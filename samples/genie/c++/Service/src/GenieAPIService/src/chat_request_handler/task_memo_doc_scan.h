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
constexpr size_t kSectionTitleChars = 24;
constexpr size_t kReadFileSuffixChars = 480;
constexpr size_t kMaxProtectedRefs = 4;
constexpr size_t kShortenPathMinChars = 64;

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
    bool marked = false;
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

inline bool IsPathChar(char c)
{
    const unsigned char u = static_cast<unsigned char>(c);
    return std::isalnum(u) || c == '_' || c == '.' || c == '/' || c == '\\' || c == ':' || c == '~' || c == '-';
}

inline bool IsPathSep(char c)
{
    return c == '/' || c == '\\';
}

inline std::string ShortenPathToken(const std::string &tok)
{
    if (tok.size() <= kShortenPathMinChars)
        return tok;
    size_t core_end = tok.size();
    while (core_end > 0 && (tok[core_end - 1] == '.' || tok[core_end - 1] == ':' || tok[core_end - 1] == '-' ||
                            IsPathSep(tok[core_end - 1])))
        --core_end;
    const std::string core = tok.substr(0, core_end);
    size_t seps = 0;
    for (char c : core)
        seps += IsPathSep(c) ? 1 : 0;
    const size_t last = core.find_last_of("/\\");
    if (seps < 3 || last == std::string::npos || last == 0)
        return tok;
    const size_t prev = core.find_last_of("/\\", last - 1);
    if (prev == std::string::npos)
        return tok;
    return "..." + core.substr(prev) + tok.substr(core_end);
}

inline std::string ShortenLongPaths(const std::string &text)
{
    std::string out;
    size_t i = 0;
    while (i < text.size())
    {
        if (!IsPathChar(text[i]))
        {
            out += text[i++];
            continue;
        }
        size_t j = i;
        while (j < text.size() && IsPathChar(text[j]))
            ++j;
        out += ShortenPathToken(text.substr(i, j - i));
        i = j;
    }
    return out;
}

inline const std::vector<std::string> &HardRuleMarkers()
{
    static const std::vector<std::string> kMarkers = {"NEVER",  "MUST",           "MANDATORY",      "REQUIRED", "Do NOT", "DO NOT",
                                                      "必须",   "Non-negotiable", "NON-NEGOTIABLE", "禁止",     "不得",   "严禁"};
    return kMarkers;
}

inline bool IsHardRuleLine(const std::string &line)
{
    for (const std::string &k : HardRuleMarkers())
        if (line.find(k) != std::string::npos)
            return true;
    return false;
}

inline bool EndsWithLabelColon(const std::string &text)
{
    size_t end = text.size();
    while (end > 0 && (text[end - 1] == '*' || text[end - 1] == '_' || text[end - 1] == ' '))
        --end;
    if (end >= 1 && text[end - 1] == ':')
        return true;
    return end >= 3 && text.compare(end - 3, 3, "\xEF\xBC\x9A") == 0;
}

inline bool HasRuleSubstance(const std::string &text)
{
    std::string rest = text;
    for (const std::string &k : HardRuleMarkers())
    {
        size_t pos = 0;
        while ((pos = rest.find(k, pos)) != std::string::npos)
            rest.replace(pos, k.size(), k.size(), ' ');
    }
    for (char c : rest)
        if (std::isalnum(static_cast<unsigned char>(c)) || static_cast<unsigned char>(c) >= 0x80)
            return true;
    return false;
}

inline std::vector<std::string> ExtractHardRules(const std::string &content, size_t max_rules, size_t cap)
{
    enum Kind
    {
        kBlank,
        kText,
        kBarrier
    };
    const std::vector<std::string> lines = SplitLines(content);
    std::vector<Kind> kinds(lines.size(), kBlank);
    std::vector<std::string> texts(lines.size());
    bool in_fence = false;
    for (size_t i = 0; i < lines.size(); ++i)
    {
        const std::string &raw = lines[i];
        if (IsFenceLine(raw))
        {
            in_fence = !in_fence;
            kinds[i] = kBarrier;
            continue;
        }
        const size_t first = raw.find_first_not_of(" \t\r>");
        if (first == std::string::npos)
            continue;
        if (raw[first] == '#' && !in_fence)
        {
            kinds[i] = kBarrier;
            continue;
        }
        const size_t body = raw.find_first_not_of(" \t\r>-*|#", first);
        if (body == std::string::npos)
            continue;
        texts[i] = Trim(raw.substr(body));
        kinds[i] = kText;
    }
    std::vector<std::string> out;
    std::vector<char> consumed(lines.size(), 0);
    for (size_t i = 0; i < lines.size() && out.size() < max_rules; ++i)
    {
        if (kinds[i] != kText || consumed[i] || !IsHardRuleLine(texts[i]))
            continue;
        std::string rule = texts[i];
        if (EndsWithLabelColon(rule))
        {
            size_t j = i + 1;
            while (j < lines.size() && kinds[j] == kBlank)
                ++j;
            if (j < lines.size() && kinds[j] == kText && !consumed[j])
            {
                rule += " " + texts[j];
                consumed[j] = 1;
            }
            else if (!HasRuleSubstance(rule))
                continue;
        }
        if (rule.size() < 8)
            continue;
        out.push_back(ClipKeepTail(ShortenLongPaths(rule), cap));
    }
    return out;
}

inline void AdmitHardRule(json &rules, json &sources, const std::string &rule, const std::string &doc, size_t cap)
{
    if (!rules.is_array())
        rules = json::array();
    if (!sources.is_array())
        sources = json::array();
    while (sources.size() < rules.size())
        sources.push_back("");
    while (sources.size() > rules.size())
        sources.erase(sources.size() - 1);
    for (const auto &r : rules)
        if (r.is_string() && r.get<std::string>() == rule)
            return;
    if (rules.size() >= cap)
    {
        std::vector<std::pair<std::string, size_t>> holders;
        for (const auto &s : sources)
        {
            const std::string name = s.is_string() ? s.get<std::string>() : std::string();
            bool found = false;
            for (auto &h : holders)
                if (h.first == name)
                {
                    ++h.second;
                    found = true;
                }
            if (!found)
                holders.emplace_back(name, 1);
        }
        size_t mine = 0;
        const std::pair<std::string, size_t> *victim = nullptr;
        for (const auto &h : holders)
        {
            if (h.first == doc)
                mine = h.second;
            if (!victim || h.second > victim->second)
                victim = &h;
        }
        if (!victim || victim->first == doc || victim->second < 2 || victim->second <= mine + 1)
            return;
        for (size_t i = rules.size(); i-- > 0;)
        {
            if (sources[i].is_string() && sources[i].get<std::string>() == victim->first)
            {
                rules.erase(i);
                sources.erase(i);
                break;
            }
        }
    }
    rules.push_back(rule);
    sources.push_back(doc);
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
            const bool marked = IsHardRuleLine(trimmed);
            const size_t body = trimmed.find_first_not_of(">-* \t");
            const std::string cite = ClipKeepTail(trimmed.substr(body == std::string::npos ? 0 : body), cite_cap);
            DocRef *same = nullptr;
            for (DocRef &r : out)
                if (r.path == path && r.section == section)
                    same = &r;
            if (same)
            {
                if (marked && !same->marked)
                {
                    same->cite = cite;
                    same->marked = true;
                }
                continue;
            }
            out.push_back({path, section, cite, marked});
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

inline bool IsMarkedRef(const json &value)
{
    return value.is_object() && value.value("marked", false);
}

inline size_t CountMarkedRefs(const json &pending)
{
    size_t n = 0;
    for (auto it = pending.begin(); it != pending.end(); ++it)
        n += IsMarkedRef(it.value()) ? 1 : 0;
    return n;
}

inline bool EraseOldestRef(json &pending, bool marked, const std::string &keep = "")
{
    for (auto it = pending.begin(); it != pending.end(); ++it)
    {
        if (it.key() == keep || IsMarkedRef(it.value()) != marked)
            continue;
        const std::string victim = it.key();
        pending.erase(victim);
        return true;
    }
    return false;
}

inline void MakeRoomForMarkedRef(json &pending, const std::string &keep)
{
    while (CountMarkedRefs(pending) >= kMaxProtectedRefs && EraseOldestRef(pending, true, keep))
    {
    }
}

inline void FoldRefs(json &pending, const std::vector<DocRef> &refs, const json &read_files,
                     const std::string &own_path, size_t cap)
{
    if (!pending.is_object())
        pending = json::object();
    for (const DocRef &ref : refs)
    {
        if (PathMatches(ref.path, own_path))
            continue;
        auto existing = pending.find(ref.path);
        if (existing != pending.end())
        {
            if (ref.marked && !IsMarkedRef(*existing))
            {
                MakeRoomForMarkedRef(pending, ref.path);
                pending[ref.path] = json{{"section", ref.section}, {"cite", ref.cite}, {"marked", true}};
            }
            continue;
        }
        bool already_read = false;
        if (read_files.is_object())
            for (auto it = read_files.begin(); it != read_files.end() && !already_read; ++it)
                already_read = PathMatches(ref.path, it.key());
        if (already_read)
            continue;
        if (ref.marked)
            MakeRoomForMarkedRef(pending, "");
        while (pending.size() >= cap && (EraseOldestRef(pending, false) || EraseOldestRef(pending, true)))
        {
        }
        pending[ref.path] = ref.marked ? json{{"section", ref.section}, {"cite", ref.cite}, {"marked", true}}
                                       : json{{"section", ref.section}, {"cite", ref.cite}};
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

inline std::string RenderSectionsSuffix(const json &file_entry, size_t budget)
{
    const std::vector<Heading> heads = LoadHeadings(file_entry);
    if (heads.empty())
        return "";
    const std::string prefix = "; sections seen: ";
    const std::string omitted = "... ";
    std::vector<std::string> items;
    for (const Heading &h : heads)
        items.push_back("L" + std::to_string(h.line) + " " + ClipHead(h.title, kSectionTitleChars));
    size_t first = 0;
    auto total_from = [&](size_t from) {
        size_t n = prefix.size() + (from > 0 ? omitted.size() : 0);
        for (size_t i = from; i < items.size(); ++i)
            n += items[i].size() + (i > from ? 2 : 0);
        return n;
    };
    while (first < items.size() && total_from(first) > budget)
        ++first;
    if (first >= items.size())
        return "";
    std::string out = prefix + (first > 0 ? omitted : std::string());
    for (size_t i = first; i < items.size(); ++i)
        out += (i > first ? "; " : "") + items[i];
    return out;
}

inline std::string RenderCursorSuffix(const json &file_entry)
{
    if (!file_entry.is_object() || !file_entry.contains("used_line") || !file_entry["used_line"].is_number_integer())
        return "";
    const size_t used = file_entry["used_line"].get<size_t>();
    const std::vector<Heading> heads = LoadHeadings(file_entry);
    std::string out = "; last action matched L" + std::to_string(used);
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
        out += " (under \"" + ClipHead(under->title, kSectionTitleChars) + "\")";
    if (next)
        out += "; next L" + std::to_string(next->line) + " \"" + ClipHead(next->title, kSectionTitleChars) + "\"";
    return out;
}

inline std::string RenderReadFileSuffix(const json &file_entry, size_t budget)
{
    std::string cursor = RenderCursorSuffix(file_entry);
    size_t sections_budget = budget;
    if (cursor.size() >= budget)
        cursor.clear();
    else
        sections_budget = budget - cursor.size();
    return RenderSectionsSuffix(file_entry, sections_budget) + cursor;
}

inline std::string DisplayPathName(const std::string &path, bool with_parent)
{
    std::string p = path;
    std::replace(p.begin(), p.end(), '\\', '/');
    while (p.size() > 1 && p.back() == '/')
        p.pop_back();
    const size_t last = p.rfind('/');
    if (last == std::string::npos)
        return p;
    if (!with_parent || last == 0)
        return p.substr(last + 1);
    const size_t prev = p.rfind('/', last - 1);
    return p.substr(prev == std::string::npos ? 0 : prev + 1);
}

inline std::vector<std::string> SplitCommandWords(const std::string &command)
{
    std::vector<std::string> words;
    std::string cur;
    char quote = 0;
    for (char c : command)
    {
        if (quote)
        {
            if (c == quote)
                quote = 0;
            else
                cur += c;
            continue;
        }
        if (c == '"' || c == '\'')
        {
            quote = c;
            continue;
        }
        if (std::isspace(static_cast<unsigned char>(c)))
        {
            if (!cur.empty())
            {
                words.push_back(cur);
                cur.clear();
            }
            continue;
        }
        cur += c;
    }
    if (!cur.empty())
        words.push_back(cur);
    return words;
}

inline std::string CompactCommand(const std::string &command)
{
    const std::vector<std::string> words = SplitCommandWords(command);
    size_t script = words.size();
    for (size_t i = 0; i < words.size() && script == words.size(); ++i)
    {
        const std::string base = Lower(DisplayPathName(words[i], false));
        const bool is_exe = base.size() > 4 && base.compare(base.size() - 4, 4, ".exe") == 0;
        if (!is_exe && HasScriptExtension(base))
            script = i;
    }
    if (script == words.size())
        return "";
    std::string exe;
    for (size_t i = script; i-- > 0 && exe.empty();)
        if (!words[i].empty() && words[i][0] != '-' && words[i][0] != '/')
            exe = DisplayPathName(words[i], false);
    std::string rest;
    for (size_t i = script + 1; i < words.size(); ++i)
        rest += (rest.empty() ? "" : " ") + words[i];
    std::string out = (exe.empty() ? std::string() : exe + " ") + DisplayPathName(words[script], false);
    if (!rest.empty())
        out += " " + ClipHead(rest, 60);
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

inline size_t CountOccurrences(const std::string &s, const char *needle)
{
    size_t n = 0;
    for (size_t pos = s.find(needle); pos != std::string::npos; pos = s.find(needle, pos + 3))
        ++n;
    return n;
}

inline std::string ExtractCodeExcerpt(const std::string &text, size_t cap)
{
    constexpr size_t kMaxCodeLines = 10;
    constexpr size_t kMaxNotes = 2;
    std::vector<std::string> code;
    std::vector<std::string> notes;
    bool in_doc = false;
    for (const std::string &raw : SplitLines(text))
    {
        const std::string t = Trim(raw);
        if (t.empty())
            continue;
        const size_t quotes = CountOccurrences(t, "\"\"\"") + CountOccurrences(t, "'''");
        if (in_doc)
        {
            if (quotes % 2 == 1)
                in_doc = false;
            continue;
        }
        if (t.rfind("\"\"\"", 0) == 0 || t.rfind("'''", 0) == 0)
        {
            in_doc = quotes % 2 == 1;
            continue;
        }
        const bool directive = t.rfind("#include", 0) == 0 || t.rfind("#define", 0) == 0 || t.rfind("#!", 0) == 0;
        const bool comment = !directive && (t[0] == '#' || t.rfind("//", 0) == 0 || t.rfind("REM ", 0) == 0 ||
                                            t.rfind("::", 0) == 0);
        if (comment)
        {
            if (notes.size() < kMaxNotes && IsHardRuleLine(t))
            {
                const size_t body = t.find_first_not_of("#/: -");
                notes.push_back(ClipHead(body == std::string::npos ? t : t.substr(body), 100));
            }
            continue;
        }
        if (code.size() < kMaxCodeLines)
            code.push_back(ClipHead(t, 110));
    }
    std::string out;
    for (const std::string &c : code)
        out += (out.empty() ? "" : " ; ") + c;
    for (size_t i = 0; i < notes.size(); ++i)
        out += (i == 0 ? " [notes: " : " | ") + notes[i];
    if (!notes.empty())
        out += "]";
    return ClipHead(out, cap);
}

inline void AppendCompletedRow(json &field, const std::string &row, size_t cap, const std::string &supersede_key = "")
{
    if (!field.is_array())
        field = json::array();
    for (const auto &existing : field)
        if (existing.is_string() && existing.get<std::string>() == row)
            return;
    if (!supersede_key.empty())
    {
        for (size_t i = field.size(); i-- > 0;)
            if (field[i].is_string() && field[i].get<std::string>().find(supersede_key) != std::string::npos)
                field.erase(field.begin() + static_cast<long long>(i));
    }
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
