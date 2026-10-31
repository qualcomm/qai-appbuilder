//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef TOOL_CALL_REPETITION_STORE_H
#define TOOL_CALL_REPETITION_STORE_H

#include "../model/model_config.h"
#include <string>
#include <unordered_map>
#include <list>
#include <mutex>
#include <cstddef>
#include <cstdint>
#include <vector>

class ToolCallRepetitionStore
{
public:

    struct SignatureRecord
    {
        enum class Outcome { kOk, kEmpty, kError };

        int total_count = 0;
        int contiguous_count = 0;
        long long first_seq = 0;
        long long last_seq = 0;
        Outcome last_outcome = Outcome::kOk;

        std::vector<uint64_t> digests;
        std::string outcome_preview;
        long long last_note_seq = 0;

        int blocked_count = 0;
    };

    struct HydratedOutcome
    {
        uint64_t digest = 0;
        SignatureRecord::Outcome cls = SignatureRecord::Outcome::kOk;
        std::string preview;
    };

    enum class RepeatGate { kAllow, kSoftRetry, kHardBlock };

    static constexpr size_t kMaxOutcomePreviewBytes = 240;

    static ToolCallRepetitionStore& GetInstance();

    void Configure(const ToolCallRepairConfig::RedundantToolCallConfig& cfg);

    static std::string MakeKey(const std::string& session_key, const std::string& model_name);

    static size_t CountRepeatsInWindow(const std::vector<std::string>& keys, size_t window);

    static RepeatGate DecideRepeatGate(int total_count, size_t distinct_digests,
                                       bool already_retried_this_request,
                                       int soft_threshold, int hard_threshold);

    static int EffectiveNonContiguousNoticeThreshold(int configured, int hard_block_threshold);

    static std::string TruncatePreviewUtf8(const std::string& text, size_t max_bytes);

    static std::string NormalizeResultForDigest(const std::string& content);
    static uint64_t ResultDigest(const std::string& content);

    int RecordCall(const std::string& key, const std::string& signature);

    int GetRepeatCount(const std::string& key) const;

    int GetNonContiguousRepeatCount(const std::string& key) const;

    int PeekTotalCount(const std::string& key, const std::string& signature) const;

    void HydrateOutcomes(const std::string& key, const std::string& signature,
                         const std::vector<HydratedOutcome>& history);

    bool GetOutcomeSummary(const std::string& key, const std::string& signature,
                           std::string& preview_out, int& total_out, bool& digest_stable_out) const;

    int PeekDistinctDigestCount(const std::string& key, const std::string& signature) const;

    bool TryMarkNote(const std::string& key, const std::string& signature);

    bool TryMarkNoteSlot(const std::string& key, const std::string& slot, uint64_t content_digest);

    std::string GetLastSignature(const std::string& key) const;

    int RecordBlocked(const std::string& key, const std::string& signature);
    int PeekBlockedCount(const std::string& key, const std::string& signature) const;

    void Clear();
    size_t Size() const;

private:
    ToolCallRepetitionStore() = default;
    ~ToolCallRepetitionStore() = default;
    ToolCallRepetitionStore(const ToolCallRepetitionStore&) = delete;
    ToolCallRepetitionStore& operator=(const ToolCallRepetitionStore&) = delete;

    struct Entry
    {
        std::string last_signature;
        int repeat_count = 0;

        long long turn_seq = 0;
        std::unordered_map<std::string, SignatureRecord> records;

        struct NoteSlot
        {
            long long last_seq = 0;
            uint64_t last_digest = 0;
        };
        std::unordered_map<std::string, NoteSlot> note_slots;
    };

    static constexpr size_t kMaxSignaturesPerEntry = 256;

    static constexpr size_t kMaxNoteSlotsPerEntry = 64;

    static SignatureRecord& TouchSignatureRecord(Entry& entry, const std::string& signature,
                                                 size_t max_signatures);

    static void PruneNoteSlots(Entry& entry);

    SignatureRecord* TouchForOutcome(const std::string& key, const std::string& signature);

    static bool ApplyOutcomeObservation(SignatureRecord& record, uint64_t digest,
                                        SignatureRecord::Outcome cls, const std::string& preview);

    const Entry* FindEntry(const std::string& key) const;

    void EvictIfNeeded();

    mutable std::mutex mutex_;
    std::list<std::string> lru_list_;
    std::unordered_map<std::string, std::pair<Entry, std::list<std::string>::iterator>> cache_;

    size_t max_entries_ = 2000;
    size_t max_signatures_per_entry_ = kMaxSignaturesPerEntry;
    int note_min_interval_turns_ = 4;
};

#endif
