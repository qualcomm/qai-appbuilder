//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef TEXT_SPLITTER_H
#define TEXT_SPLITTER_H

#include <string>

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
        {
            return static_cast<size_t>(s.length());
        }
    ) : _separators(separators), _keep_separator(keep_separator),
        _chunk_size(chunk_size), _length_function(length_function)
    {}

    std::vector<std::string> split_text(const std::string &text)
    {
        return _split_text(text, _separators);
    }

    static inline const int DOCS_MAX_SIZE = DEFAULT_CONTEXT_SIZE - 1024;
    const int DOCS_MAX_QUERY_TIMES = 3;
};

#endif //TEXT_SPLITTER_H
