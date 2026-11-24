//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef RELEVANCE_LATCH_H
#define RELEVANCE_LATCH_H

#include <cstddef>
#include <cstdint>
#include <map>
#include <mutex>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

class RelevanceLatch
{
public:
    struct Session
    {
        std::map<std::string, int> skill_levels;
        std::vector<std::string> skill_order;
        std::set<std::string> tools;
    };

    static RelevanceLatch& GetInstance()
    {
        static RelevanceLatch instance;
        return instance;
    }

    bool Snapshot(const std::string& key, Session& out)
    {
        if (key.empty())
            return false;
        std::lock_guard<std::mutex> lock(mutex_);
        auto it = sessions_.find(key);
        if (it == sessions_.end())
            return false;
        it->second.touched = ++clock_;
        out = it->second.session;
        return true;
    }

    void Commit(const std::string& key, const Session& session)
    {
        if (key.empty())
            return;
        std::lock_guard<std::mutex> lock(mutex_);
        Entry& entry = sessions_[key];
        entry.session = session;
        entry.touched = ++clock_;
        if (sessions_.size() <= kMaxSessions)
            return;
        auto oldest = sessions_.begin();
        for (auto it = sessions_.begin(); it != sessions_.end(); ++it) {
            if (it->second.touched < oldest->second.touched)
                oldest = it;
        }
        sessions_.erase(oldest);
    }

private:
    struct Entry
    {
        Session session;
        uint64_t touched = 0;
    };

    static constexpr size_t kMaxSessions = 64;

    std::mutex mutex_;
    std::unordered_map<std::string, Entry> sessions_;
    uint64_t clock_ = 0;
};

#endif
