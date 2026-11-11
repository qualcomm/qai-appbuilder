//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef LOG_TABLE_H
#define LOG_TABLE_H

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <string>
#include <utility>
#include <vector>

class LogTable
{
public:
    enum class Align
    {
        kLeft,
        kRight,
    };

    explicit LogTable(std::string title = {}) : title_{Normalize(std::move(title))} {}

    LogTable &SetTitle(std::string title)
    {
        title_ = Normalize(std::move(title));
        return *this;
    }

    LogTable &SetHeader(std::vector<std::string> header, std::vector<Align> aligns = {})
    {
        for (auto &cell: header)
        {
            cell = Normalize(std::move(cell));
        }
        header_ = std::move(header);
        aligns_ = std::move(aligns);
        return *this;
    }

    LogTable &AddRow(std::vector<std::string> cells)
    {
        for (auto &cell: cells)
        {
            cell = Normalize(std::move(cell));
        }
        rows_.push_back(std::move(cells));
        return *this;
    }

    std::string Str() const
    {
        size_t cols = header_.size();
        for (const auto &row: rows_)
        {
            cols = std::max(cols, row.size());
        }

        const size_t title_width = DisplayWidth(title_);
        if (cols == 0 && title_width == 0)
        {
            return {};
        }

        std::vector<size_t> widths(cols, 0);
        Measure(header_, widths);
        for (const auto &row: rows_)
        {
            Measure(row, widths);
        }

        size_t span = 0;
        for (const auto width: widths)
        {
            span += width;
        }
        span += cols > 1 ? 3 * (cols - 1) : 0;
        if (title_width > span)
        {
            if (cols > 0)
            {
                widths.back() += title_width - span;
            }
            span = title_width;
        }

        const std::string full_border = "+" + std::string(span + 2, '-') + "+\n";
        std::string col_border = "+";
        for (const auto width: widths)
        {
            col_border += std::string(width + 2, '-') + "+";
        }
        col_border += "\n";

        std::string out;
        if (title_width > 0)
        {
            out += full_border;
            out += "| " + Pad(title_, span, Align::kLeft) + " |\n";
            out += cols > 0 ? col_border : full_border;
        }
        else
        {
            out += col_border;
        }

        if (!header_.empty())
        {
            out += FormatRow(header_, widths);
            out += col_border;
        }
        for (const auto &row: rows_)
        {
            out += FormatRow(row, widths);
        }
        if (cols > 0)
        {
            out += col_border;
        }
        return out;
    }

    static std::string Fixed(double value, int precision = 2)
    {
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%.*f", precision, value);
        return buf;
    }

    template<typename Json>
    static std::string JsonText(const Json &value)
    {
        return value.is_string() ? value.template get<std::string>() : value.dump();
    }

private:
    static constexpr size_t kMaxCellWidth = 160;

    std::string title_;
    std::vector<std::string> header_;
    std::vector<Align> aligns_;
    std::vector<std::vector<std::string>> rows_;

    Align AlignOf(size_t col) const
    {
        return col < aligns_.size() ? aligns_[col] : Align::kLeft;
    }

    static void Measure(const std::vector<std::string> &cells, std::vector<size_t> &widths)
    {
        for (size_t i = 0; i < cells.size(); ++i)
        {
            widths[i] = std::max(widths[i], DisplayWidth(cells[i]));
        }
    }

    std::string FormatRow(const std::vector<std::string> &cells, const std::vector<size_t> &widths) const
    {
        std::string line = "|";
        for (size_t i = 0; i < widths.size(); ++i)
        {
            line += " " + Pad(i < cells.size() ? cells[i] : std::string{}, widths[i], AlignOf(i)) + " |";
        }
        line += "\n";
        return line;
    }

    static std::string Pad(const std::string &cell, size_t width, Align align)
    {
        const size_t cell_width = DisplayWidth(cell);
        const std::string fill(width > cell_width ? width - cell_width : 0, ' ');
        return align == Align::kRight ? fill + cell : cell + fill;
    }

    static size_t DecodeUtf8(const std::string &text, size_t pos, uint32_t &code_point)
    {
        const auto lead = static_cast<unsigned char>(text[pos]);
        size_t len = 1;
        uint32_t cp = lead;
        if (lead >= 0xC2 && lead <= 0xDF)
        {
            len = 2;
            cp = lead & 0x1F;
        }
        else if (lead >= 0xE0 && lead <= 0xEF)
        {
            len = 3;
            cp = lead & 0x0F;
        }
        else if (lead >= 0xF0 && lead <= 0xF4)
        {
            len = 4;
            cp = lead & 0x07;
        }

        if (len == 1 || pos + len > text.size())
        {
            code_point = lead;
            return 1;
        }
        for (size_t i = 1; i < len; ++i)
        {
            const auto next = static_cast<unsigned char>(text[pos + i]);
            if ((next & 0xC0) != 0x80)
            {
                code_point = lead;
                return 1;
            }
            cp = (cp << 6) | (next & 0x3F);
        }
        code_point = cp;
        return len;
    }

    static size_t CodePointWidth(uint32_t cp)
    {
        if ((cp >= 0x0300 && cp <= 0x036F) || (cp >= 0x200B && cp <= 0x200F) || (cp >= 0xFE00 && cp <= 0xFE0F))
        {
            return 0;
        }
        const bool wide = (cp >= 0x1100 && cp <= 0x115F) || (cp >= 0x2E80 && cp <= 0x303E) ||
                          (cp >= 0x3041 && cp <= 0x33FF) || (cp >= 0x3400 && cp <= 0x4DBF) ||
                          (cp >= 0x4E00 && cp <= 0x9FFF) || (cp >= 0xA000 && cp <= 0xA4CF) ||
                          (cp >= 0xAC00 && cp <= 0xD7A3) || (cp >= 0xF900 && cp <= 0xFAFF) ||
                          (cp >= 0xFE30 && cp <= 0xFE6F) || (cp >= 0xFF00 && cp <= 0xFF60) ||
                          (cp >= 0xFFE0 && cp <= 0xFFE6) || (cp >= 0x1F300 && cp <= 0x1F64F) ||
                          (cp >= 0x1F900 && cp <= 0x1F9FF) || (cp >= 0x20000 && cp <= 0x3FFFD);
        return wide ? 2 : 1;
    }

    static size_t DisplayWidth(const std::string &text)
    {
        size_t width = 0;
        for (size_t pos = 0; pos < text.size();)
        {
            uint32_t cp = 0;
            pos += DecodeUtf8(text, pos, cp);
            width += CodePointWidth(cp);
        }
        return width;
    }

    static std::string Normalize(std::string text)
    {
        for (auto &ch: text)
        {
            const auto byte = static_cast<unsigned char>(ch);
            if (byte < 0x20 || byte == 0x7F)
            {
                ch = ' ';
            }
        }
        if (DisplayWidth(text) <= kMaxCellWidth)
        {
            return text;
        }

        static constexpr size_t kEllipsisWidth = 3;
        size_t width = 0;
        size_t pos = 0;
        while (pos < text.size())
        {
            uint32_t cp = 0;
            const size_t len = DecodeUtf8(text, pos, cp);
            const size_t cp_width = CodePointWidth(cp);
            if (width + cp_width > kMaxCellWidth - kEllipsisWidth)
            {
                break;
            }
            width += cp_width;
            pos += len;
        }
        text.resize(pos);
        text += "...";
        return text;
    }
};

#endif
