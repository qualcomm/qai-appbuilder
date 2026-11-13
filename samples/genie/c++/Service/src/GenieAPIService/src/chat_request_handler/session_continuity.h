//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef SESSION_CONTINUITY_H
#define SESSION_CONTINUITY_H

#include <nlohmann/json.hpp>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <unordered_map>

template <typename State>
class LruSessionTable
{
public:
    explicit LruSessionTable(size_t capacity) : capacity_(capacity == 0 ? 1 : capacity) {}

    State& Touch(const std::string& key)
    {
        auto it = entries_.find(key);
        if (it == entries_.end()) {
            EvictIfFull();
            it = entries_.emplace(key, Entry{}).first;
        }
        it->second.last_use = ++clock_;
        return it->second.state;
    }

    State* Find(const std::string& key)
    {
        auto it = entries_.find(key);
        if (it == entries_.end())
            return nullptr;
        it->second.last_use = ++clock_;
        return &it->second.state;
    }

    size_t Size() const
    {
        return entries_.size();
    }

private:
    struct Entry {
        State state{};
        uint64_t last_use = 0;
    };

    void EvictIfFull()
    {
        if (entries_.size() < capacity_)
            return;
        auto victim = entries_.begin();
        for (auto it = entries_.begin(); it != entries_.end(); ++it) {
            if (it->second.last_use < victim->second.last_use)
                victim = it;
        }
        entries_.erase(victim);
    }

    size_t capacity_;
    uint64_t clock_ = 0;
    std::unordered_map<std::string, Entry> entries_;
};

struct ContinuityMark {
    size_t size = 0;
    size_t hash = 0;
};

inline ContinuityMark MarkMessagePrefix(const nlohmann::ordered_json& messages, size_t upto)
{
    ContinuityMark mark;
    if (!messages.is_array())
        return mark;
    if (upto > messages.size())
        upto = messages.size();
    const std::hash<std::string> hasher;
    size_t h = 0;
    for (size_t i = 0; i < upto; ++i)
        h ^= hasher(messages[i].dump()) + 0x9e3779b9 + (h << 6) + (h >> 2);
    mark.size = upto;
    mark.hash = h;
    return mark;
}

inline bool ContinuesFrom(const nlohmann::ordered_json& messages, const ContinuityMark& prev)
{
    if (prev.size == 0)
        return true;
    if (!messages.is_array() || messages.size() < prev.size)
        return false;
    const ContinuityMark now = MarkMessagePrefix(messages, prev.size);
    return now.hash == prev.hash;
}

template <typename State>
State& TouchContinuing(LruSessionTable<State>& table, const std::string& key, const nlohmann::ordered_json& messages)
{
    State& st = table.Touch(key);
    if (!ContinuesFrom(messages, st.mark))
        st = State{};
    st.mark = MarkMessagePrefix(messages, messages.is_array() ? messages.size() : 0);
    return st;
}

#endif
