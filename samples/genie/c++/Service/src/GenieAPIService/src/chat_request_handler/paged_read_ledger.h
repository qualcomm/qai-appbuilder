//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef PAGED_READ_LEDGER_H
#define PAGED_READ_LEDGER_H

#include "task_memo_doc_scan.h"
#include <algorithm>
#include <cstddef>
#include <map>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

class PagedReadLedger
{
public:
    void Observe(const std::string& path, const std::string& content)
    {
        const std::string key = task_memo_scan::NormalizePath(path);
        size_t first = 0, last = 0, total = 0;
        if (key.empty() || !task_memo_scan::ParsePagedReadFooter(content, first, last, total))
            return;
        if (first == 0 || last < first || last > total)
            return;
        Entry& entry = entries_[key];
        if (entry.total != total) {
            entry = Entry();
            entry.total = total;
        }
        AddRange(entry.ranges, first, last);
        const task_memo_scan::PageText page = task_memo_scan::ExtractPageText(content, first);
        for (const task_memo_scan::Heading& heading : task_memo_scan::ExtractHeadings(page.text, page.first)) {
            if (!IsPlainTitle(heading.title))
                continue;
            entry.headings.emplace(heading.line, heading.title);
        }
    }

    void Invalidate(const std::string& path)
    {
        entries_.erase(task_memo_scan::NormalizePath(path));
    }

    bool RewriteFooter(const std::string& path, const std::string& content, std::string& out) const
    {
        size_t first = 0, last = 0, total = 0;
        if (!task_memo_scan::ParsePagedReadFooter(content, first, last, total))
            return false;
        if (first > 1 || last >= total)
            return false;
        const auto it = entries_.find(task_memo_scan::NormalizePath(path));
        if (it == entries_.end() || it->second.total != total || !Covers(it->second.ranges, total))
            return false;
        const std::string directory = BuildDirectory(it->second.headings, last);
        if (directory.empty())
            return false;
        const size_t marker = content.rfind("showed lines ");
        const size_t line_end = content.find('\n', marker);
        const size_t limit = line_end == std::string::npos ? content.size() : line_end;
        const size_t next = content.find("; next offset=", marker);
        if (next == std::string::npos || next > limit)
            return false;
        const size_t close = content.find(']', next);
        if (close == std::string::npos || close > limit)
            return false;
        out = content.substr(0, next) + "; this file was already shown in full earlier in this task, do not page "
              "from the top again; read a section by its line as offset: " + directory + content.substr(close);
        return true;
    }

private:
    struct Entry
    {
        size_t total = 0;
        std::vector<std::pair<size_t, size_t>> ranges;
        std::map<size_t, std::string> headings;
    };

    static constexpr size_t kTocTitleChars = 24;
    static constexpr size_t kTocMaxBytes = 360;

    static bool IsPlainTitle(const std::string& title)
    {
        return title.find_first_of("[]<>") == std::string::npos;
    }

    static void AddRange(std::vector<std::pair<size_t, size_t>>& ranges, size_t first, size_t last)
    {
        ranges.emplace_back(first, last);
        std::sort(ranges.begin(), ranges.end());
        std::vector<std::pair<size_t, size_t>> merged;
        for (const auto& range : ranges) {
            if (!merged.empty() && range.first <= merged.back().second + 1)
                merged.back().second = std::max(merged.back().second, range.second);
            else
                merged.push_back(range);
        }
        ranges.swap(merged);
    }

    static bool Covers(const std::vector<std::pair<size_t, size_t>>& ranges, size_t total)
    {
        return ranges.size() == 1 && ranges.front().first <= 1 && ranges.front().second >= total;
    }

    static std::string BuildDirectory(const std::map<size_t, std::string>& headings, size_t after_line)
    {
        std::string out;
        for (const auto& heading : headings) {
            if (heading.first <= after_line)
                continue;
            const std::string item = "L" + std::to_string(heading.first) + " " +
                                     task_memo_scan::ClipHead(heading.second, kTocTitleChars);
            const size_t separator = out.empty() ? 0 : 2;
            if (out.size() + separator + item.size() > kTocMaxBytes)
                break;
            if (separator > 0)
                out += ", ";
            out += item;
        }
        return out;
    }

    std::unordered_map<std::string, Entry> entries_;
};

#endif
