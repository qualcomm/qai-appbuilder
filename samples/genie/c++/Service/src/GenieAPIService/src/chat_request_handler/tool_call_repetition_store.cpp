//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "tool_call_repetition_store.h"
#include <algorithm>
#include <tuple>
#include <unordered_set>

namespace
{

bool IsAsciiDigit(char c)
{
    return c >= '0' && c <= '9';
}

bool IsHexDigitChar(char c)
{
    return IsAsciiDigit(c) || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

bool IsPathSeparator(char c)
{
    return c == '/' || c == '\\';
}

char LowerAscii(char c)
{
    return (c >= 'A' && c <= 'Z') ? static_cast<char>(c - 'A' + 'a') : c;
}

bool LooksLikeTempPath(const std::string& token)
{
    bool has_separator = false;
    for (char c : token)
    {
        if (IsPathSeparator(c))
        {
            has_separator = true;
            break;
        }
    }
    if (!has_separator)
        return false;

    size_t pos = 0;
    while (pos <= token.size())
    {
        size_t end = pos;
        while (end < token.size() && !IsPathSeparator(token[end]))
            ++end;
        const size_t len = end - pos;
        if (len == 3 || len == 4)
        {
            std::string component;
            component.reserve(len);
            for (size_t i = pos; i < end; ++i)
                component += LowerAscii(token[i]);
            if (component == "tmp" || component == "temp")
                return true;
        }
        if (end >= token.size())
            break;
        pos = end + 1;
    }
    return false;
}

std::string NormalizeTokenForDigest(const std::string& token)
{
    std::string out;
    out.reserve(token.size());
    size_t i = 0;
    while (i < token.size())
    {
        if (IsHexDigitChar(token[i]))
        {
            size_t j = i;
            bool has_digit = false;
            while (j < token.size() && IsHexDigitChar(token[j]))
            {
                has_digit = has_digit || IsAsciiDigit(token[j]);
                ++j;
            }
            if (has_digit && (j - i) >= 8)
            {
                out += '#';
                i = j;
                continue;
            }
        }
        if (IsAsciiDigit(token[i]))
        {
            while (i < token.size() && IsAsciiDigit(token[i]))
                ++i;
            out += '#';
            continue;
        }
        out += token[i];
        ++i;
    }
    return out;
}

bool IsSpaceChar(char c)
{
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v';
}

uint64_t Fnv1a64(const std::string& s)
{
    uint64_t hash = 1469598103934665603ULL;
    for (unsigned char c : s)
    {
        hash ^= static_cast<uint64_t>(c);
        hash *= 1099511628211ULL;
    }
    return hash;
}

}

ToolCallRepetitionStore& ToolCallRepetitionStore::GetInstance()
{
    static ToolCallRepetitionStore instance;
    return instance;
}

void ToolCallRepetitionStore::Configure(const ToolCallRepairConfig::RedundantToolCallConfig& cfg)
{
    std::lock_guard<std::mutex> lock(mutex_);
    max_signatures_per_entry_ = cfg.max_signatures_per_entry > 0
        ? static_cast<size_t>(cfg.max_signatures_per_entry)
        : kMaxSignaturesPerEntry;
    note_min_interval_turns_ = cfg.note_min_interval_turns > 0 ? cfg.note_min_interval_turns : 1;
}

std::string ToolCallRepetitionStore::MakeKey(const std::string& session_key, const std::string& model_name)
{
    if (session_key.empty() || model_name.empty())
        return "";
    return session_key + "::" + model_name;
}

int ToolCallRepetitionStore::EffectiveNonContiguousNoticeThreshold(int configured, int hard_block_threshold)
{
    if (configured <= 0 || hard_block_threshold <= 1)
        return configured;
    return std::max(1, std::min(configured, hard_block_threshold - 1));
}

ToolCallRepetitionStore::RepeatGate ToolCallRepetitionStore::DecideRepeatGate(
        int total_count, size_t distinct_digests, bool already_retried_this_request,
        int soft_threshold, int hard_threshold)
{

    if (distinct_digests >= 2)
        return RepeatGate::kAllow;

    if (distinct_digests == 0)
        return RepeatGate::kAllow;
    if (hard_threshold > 0 && total_count >= hard_threshold)
        return RepeatGate::kHardBlock;

    if (soft_threshold > 0 && total_count >= soft_threshold && !already_retried_this_request)
        return RepeatGate::kSoftRetry;
    return RepeatGate::kAllow;
}

std::string ToolCallRepetitionStore::NormalizeResultForDigest(const std::string& content)
{
    std::string out;
    out.reserve(content.size());
    size_t i = 0;
    while (i < content.size())
    {
        if (IsSpaceChar(content[i]))
        {
            ++i;
            continue;
        }
        size_t end = i;
        while (end < content.size() && !IsSpaceChar(content[end]))
            ++end;
        const std::string token = content.substr(i, end - i);
        if (!out.empty())
            out += ' ';
        out += LooksLikeTempPath(token) ? std::string("<tmp>") : NormalizeTokenForDigest(token);
        i = end;
    }
    return out;
}

uint64_t ToolCallRepetitionStore::ResultDigest(const std::string& content)
{
    return Fnv1a64(NormalizeResultForDigest(content));
}

std::string ToolCallRepetitionStore::TruncatePreviewUtf8(const std::string& text, size_t max_bytes)
{
    if (text.size() <= max_bytes)
        return text;

    static const char kEllipsis[] = "...";
    const size_t suffix_len = 3;
    if (max_bytes <= suffix_len)
        return std::string(kEllipsis).substr(0, max_bytes);

    size_t cut = max_bytes - suffix_len;
    while (cut > 0 && (static_cast<unsigned char>(text[cut]) & 0xC0) == 0x80)
        --cut;
    return text.substr(0, cut) + kEllipsis;
}

size_t ToolCallRepetitionStore::CountRepeatsInWindow(const std::vector<std::string>& keys, size_t window)
{
    if (window == 0 || keys.empty())
        return 0;
    const size_t begin = keys.size() > window ? keys.size() - window : 0;
    std::unordered_set<std::string> seen(keys.begin(), keys.begin() + begin);
    size_t repeats = 0;
    for (size_t i = begin; i < keys.size(); ++i)
    {
        if (!keys[i].empty() && !seen.insert(keys[i]).second)
            ++repeats;
    }
    return repeats;
}

ToolCallRepetitionStore::SignatureRecord& ToolCallRepetitionStore::TouchSignatureRecord(
        Entry& entry, const std::string& signature, size_t max_signatures)
{
    auto existing = entry.records.find(signature);
    if (existing != entry.records.end())
        return existing->second;

    const size_t cap = max_signatures > 0 ? max_signatures : kMaxSignaturesPerEntry;
    while (entry.records.size() >= cap && !entry.records.empty())
    {

        auto victim = entry.records.end();
        std::tuple<int, int, long long> worst;
        for (auto it = entry.records.begin(); it != entry.records.end(); ++it)
        {
            const auto score = std::make_tuple(
                it->second.last_outcome == SignatureRecord::Outcome::kError ? 1 : 0,
                it->second.total_count,
                it->second.last_seq);
            if (victim == entry.records.end() || score < worst)
            {
                victim = it;
                worst = score;
            }
        }
        if (victim == entry.records.end())
            break;
        entry.records.erase(victim);
    }
    return entry.records[signature];
}

const ToolCallRepetitionStore::Entry* ToolCallRepetitionStore::FindEntry(const std::string& key) const
{
    auto it = cache_.find(key);
    return it == cache_.end() ? nullptr : &it->second.first;
}

int ToolCallRepetitionStore::RecordCall(const std::string& key, const std::string& signature)
{
    if (key.empty())
        return 0;

    std::lock_guard<std::mutex> lock(mutex_);

    auto it = cache_.find(key);
    if (it == cache_.end())
    {
        EvictIfNeeded();
        Entry fresh;
        lru_list_.push_front(key);
        it = cache_.emplace(key, std::make_pair(std::move(fresh), lru_list_.begin())).first;
    }
    else
    {
        lru_list_.erase(it->second.second);
        lru_list_.push_front(key);
        it->second.second = lru_list_.begin();

        Entry& existing = it->second.first;
        if (existing.last_signature != signature)
            existing.repeat_count = 0;
    }

    Entry& entry = it->second.first;
    entry.repeat_count += 1;
    entry.last_signature = signature;

    ++entry.turn_seq;
    SignatureRecord& record = TouchSignatureRecord(entry, signature, max_signatures_per_entry_);
    if (record.total_count == 0)
    {
        record.total_count = 1;
        record.contiguous_count = 1;
        record.first_seq = entry.turn_seq;
    }
    else
    {
        record.total_count += 1;
        record.contiguous_count = (record.last_seq + 1 == entry.turn_seq) ? record.contiguous_count + 1 : 1;
    }
    record.last_seq = entry.turn_seq;
    return entry.repeat_count;
}

int ToolCallRepetitionStore::PeekTotalCount(const std::string& key, const std::string& signature) const
{
    if (key.empty())
        return 0;

    std::lock_guard<std::mutex> lock(mutex_);
    const Entry* entry = FindEntry(key);
    if (!entry)
        return 1;

    auto it = entry->records.find(signature);
    return it == entry->records.end() ? 1 : it->second.total_count + 1;
}

ToolCallRepetitionStore::SignatureRecord* ToolCallRepetitionStore::TouchForOutcome(
        const std::string& key, const std::string& signature)
{
    auto it = cache_.find(key);
    if (it == cache_.end())
    {
        EvictIfNeeded();
        Entry fresh;
        lru_list_.push_front(key);
        it = cache_.emplace(key, std::make_pair(std::move(fresh), lru_list_.begin())).first;
    }
    else
    {
        lru_list_.erase(it->second.second);
        lru_list_.push_front(key);
        it->second.second = lru_list_.begin();
    }

    Entry& entry = it->second.first;
    return &TouchSignatureRecord(entry, signature, max_signatures_per_entry_);
}

bool ToolCallRepetitionStore::ApplyOutcomeObservation(SignatureRecord& record, uint64_t digest,
                                                     SignatureRecord::Outcome cls,
                                                     const std::string& preview)
{

    const bool had_prior = !record.digests.empty();
    const bool world_changed = had_prior && (record.digests.front() != digest || record.last_outcome != cls);
    record.digests.insert(record.digests.begin(), digest);
    if (record.digests.size() > 2)
        record.digests.resize(2);
    record.last_outcome = cls;
    record.outcome_preview = TruncatePreviewUtf8(preview, kMaxOutcomePreviewBytes);
    return world_changed;
}

void ToolCallRepetitionStore::HydrateOutcomes(const std::string& key, const std::string& signature,
                                              const std::vector<HydratedOutcome>& history)
{
    if (key.empty() || signature.empty() || history.empty())
        return;

    std::lock_guard<std::mutex> lock(mutex_);
    SignatureRecord* record = TouchForOutcome(key, signature);
    if (!record)
        return;

    const int previous_total = record->total_count;

    record->digests.clear();
    record->outcome_preview.clear();
    record->last_outcome = SignatureRecord::Outcome::kOk;
    record->total_count = 0;

    bool ever_changed = false;
    for (const auto& observation : history)
    {
        record->total_count += 1;
        if (ApplyOutcomeObservation(*record, observation.digest, observation.cls, observation.preview))
        {
            record->total_count = 1;
            record->blocked_count = 0;
            ever_changed = true;
        }
    }

    if (!ever_changed && previous_total > record->total_count)
        record->total_count = previous_total;
}

int ToolCallRepetitionStore::PeekDistinctDigestCount(const std::string& key,
                                                     const std::string& signature) const
{
    if (key.empty())
        return 0;

    std::lock_guard<std::mutex> lock(mutex_);
    const Entry* entry = FindEntry(key);
    if (!entry)
        return 0;

    auto it = entry->records.find(signature);
    if (it == entry->records.end() || it->second.digests.empty())
        return 0;
    return (it->second.digests.size() >= 2 && it->second.digests[0] != it->second.digests[1]) ? 2 : 1;
}

bool ToolCallRepetitionStore::GetOutcomeSummary(const std::string& key, const std::string& signature,
                                                std::string& preview_out, int& total_out,
                                                bool& digest_stable_out) const
{
    preview_out.clear();
    total_out = 0;
    digest_stable_out = true;
    if (key.empty())
        return false;

    std::lock_guard<std::mutex> lock(mutex_);
    const Entry* entry = FindEntry(key);
    if (!entry)
        return false;

    auto it = entry->records.find(signature);
    if (it == entry->records.end())
        return false;

    preview_out = it->second.outcome_preview;
    total_out = it->second.total_count;
    digest_stable_out = it->second.digests.size() < 2 || it->second.digests[0] == it->second.digests[1];
    return true;
}

int ToolCallRepetitionStore::RecordBlocked(const std::string& key, const std::string& signature)
{
    if (key.empty() || signature.empty())
        return 0;

    std::lock_guard<std::mutex> lock(mutex_);
    auto it = cache_.find(key);
    if (it == cache_.end())
        return 0;

    Entry& entry = it->second.first;
    auto rit = entry.records.find(signature);
    if (rit == entry.records.end())
        return 0;

    rit->second.blocked_count += 1;
    return rit->second.blocked_count;
}

int ToolCallRepetitionStore::PeekBlockedCount(const std::string& key, const std::string& signature) const
{
    if (key.empty())
        return 0;

    std::lock_guard<std::mutex> lock(mutex_);
    const Entry* entry = FindEntry(key);
    if (!entry)
        return 0;

    auto it = entry->records.find(signature);
    return it == entry->records.end() ? 0 : it->second.blocked_count;
}

bool ToolCallRepetitionStore::TryMarkNote(const std::string& key, const std::string& signature)
{
    if (key.empty())
        return false;

    std::lock_guard<std::mutex> lock(mutex_);
    auto it = cache_.find(key);
    if (it == cache_.end())
        return false;

    Entry& entry = it->second.first;
    auto rit = entry.records.find(signature);
    if (rit == entry.records.end())
        return false;

    const long long min_gap = note_min_interval_turns_ > 0 ? note_min_interval_turns_ : 1;
    if (rit->second.last_note_seq != 0 && entry.turn_seq - rit->second.last_note_seq < min_gap)
        return false;

    rit->second.last_note_seq = entry.turn_seq;
    return true;
}

bool ToolCallRepetitionStore::TryMarkNoteSlot(const std::string& key, const std::string& slot,
                                              uint64_t content_digest)
{
    if (key.empty() || slot.empty())
        return false;

    std::lock_guard<std::mutex> lock(mutex_);
    auto it = cache_.find(key);
    if (it == cache_.end())
        return false;

    Entry& entry = it->second.first;
    Entry::NoteSlot& state = entry.note_slots[slot];
    const long long min_gap = note_min_interval_turns_ > 0 ? note_min_interval_turns_ : 1;
    if (state.last_seq != 0 && entry.turn_seq - state.last_seq < min_gap)
    {
        PruneNoteSlots(entry);
        return false;
    }

    if (state.last_seq != 0 && state.last_digest == content_digest)
    {
        PruneNoteSlots(entry);
        return false;
    }

    state.last_seq = entry.turn_seq;
    state.last_digest = content_digest;
    PruneNoteSlots(entry);
    return true;
}

std::string ToolCallRepetitionStore::GetLastSignature(const std::string& key) const
{
    if (key.empty())
        return "";

    std::lock_guard<std::mutex> lock(mutex_);
    const Entry* entry = FindEntry(key);
    return entry ? entry->last_signature : std::string();
}

void ToolCallRepetitionStore::PruneNoteSlots(Entry& entry)
{
    while (entry.note_slots.size() > kMaxNoteSlotsPerEntry)
    {
        auto victim = entry.note_slots.begin();
        for (auto it = entry.note_slots.begin(); it != entry.note_slots.end(); ++it)
        {
            if (it->second.last_seq < victim->second.last_seq)
                victim = it;
        }
        entry.note_slots.erase(victim);
    }
}

int ToolCallRepetitionStore::GetRepeatCount(const std::string& key) const
{
    if (key.empty())
        return 0;

    std::lock_guard<std::mutex> lock(mutex_);
    const Entry* entry = FindEntry(key);
    if (!entry)
        return 0;

    return entry->repeat_count;
}

int ToolCallRepetitionStore::GetNonContiguousRepeatCount(const std::string& key) const
{
    if (key.empty())
        return 0;

    std::lock_guard<std::mutex> lock(mutex_);
    const Entry* entry = FindEntry(key);
    if (!entry)
        return 0;

    auto it = entry->records.find(entry->last_signature);
    return it == entry->records.end() ? 0 : it->second.total_count;
}

void ToolCallRepetitionStore::Clear()
{
    std::lock_guard<std::mutex> lock(mutex_);
    cache_.clear();
    lru_list_.clear();
}

size_t ToolCallRepetitionStore::Size() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return cache_.size();
}

void ToolCallRepetitionStore::EvictIfNeeded()
{
    while (!lru_list_.empty() && cache_.size() >= max_entries_)
    {
        const std::string& oldest_key = lru_list_.back();
        cache_.erase(oldest_key);
        lru_list_.pop_back();
    }
}
