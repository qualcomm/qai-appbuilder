//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "message_pre_filter.h"
#include "content_condenser.h"
#include "../processor/harmony.h"
#include "../gateway/security/security_utils.h"
#include <utils.h>
#include "log.h"
#include <map>
#include <sstream>
#include <algorithm>
#include <cstdio>
#include <regex>
#include <unordered_set>
#include <mutex>
#include <functional>
#include <limits>

using json = nlohmann::ordered_json;

namespace {

std::string CanonicalToolCallKey(const json& tool_call)
{
    if (!tool_call.is_object() || !tool_call.contains("function") || !tool_call["function"].is_object())
        return "";
    const json& fn = tool_call["function"];
    const std::string name = fn.value("name", std::string());
    if (name.empty()) return "";
    std::string args;
    if (fn.contains("arguments")) {
        nlohmann::json parsed;
        if (fn["arguments"].is_string()) {
            const std::string raw = fn["arguments"].get<std::string>();
            parsed = nlohmann::json::parse(raw, nullptr, false);
            if (parsed.is_discarded()) return name + "\x01" + raw;
        } else {
            parsed = nlohmann::json(fn["arguments"]);
        }
        if (parsed.is_object()) {
            nlohmann::json norm = nlohmann::json::object();
            for (auto it = parsed.begin(); it != parsed.end(); ++it) {
                if (it.value().is_null()) continue;
                const bool path_like = it.value().is_string() && [&]() {
                    const std::string& s = it.value().get_ref<const std::string&>();
                    return !s.empty() && s.find_first_of(" \t\r\n") == std::string::npos &&
                           s.find_first_of("/\\") != std::string::npos;
                }();
                if (path_like) {
                    std::string p = it.value().get<std::string>();
                    std::replace(p.begin(), p.end(), '/', '\\');
                    norm[it.key()] = p;
                } else {
                    norm[it.key()] = it.value();
                }
            }
            parsed = std::move(norm);
        }
        args = parsed.dump();
    }
    return name + "\x01" + args;
}

constexpr float kEvictionLowWaterRatio = 0.5f;
constexpr float kEvictionHighWaterRatio = 0.95f;
constexpr size_t kMaxEvictionStates = 64;
constexpr float kToolEntryCapRatio = 0.3f;
constexpr float kToolEntryHeadShare = 0.6f;
constexpr float kToolEntryHardCapRatio = 0.09375f;
constexpr float kToolEntryErrorShare = 0.25f;
const char kToolEntryErrorsHeader[] = "[error lines from truncated part]\n";

std::string OmittedMarker(const std::string& content, size_t head_end, size_t tail_begin)
{
    const size_t first = static_cast<size_t>(std::count(content.begin(), content.begin() + head_end, '\n')) + 1;
    const size_t last = static_cast<size_t>(std::count(content.begin(), content.begin() + tail_begin, '\n'));
    const size_t total = static_cast<size_t>(std::count(content.begin(), content.end(), '\n')) + 1;
    return "\n[... output truncated: lines " + std::to_string(first) + "-" + std::to_string(std::max(first, last)) +
           " of " + std::to_string(total) + " dropped by context compression; repeating this same read returns a "
           "result that gets compressed the same way, so continue with the content shown here ...]\n";
}

bool IsErrorLine(const std::string& line)
{
    static const char* kSignals[] = {"error", "Error", "ERROR", "Traceback", "Exception", "exception",
                                     "failed", "Failed", "FAILED", "not found", "No such file"};
    for (const char* s : kSignals) {
        if (line.find(s) != std::string::npos) return true;
    }
    return false;
}

size_t Utf8Floor(const std::string& s, size_t pos)
{
    if (pos >= s.size()) return s.size();
    while (pos > 0 && (static_cast<unsigned char>(s[pos]) & 0xC0) == 0x80) --pos;
    return pos;
}

bool CapPagedReadHead(const std::string& content, size_t keep_bytes, std::string& out)
{
    constexpr size_t kFooterReserve = 200;
    const size_t marker = content.rfind("showed lines ");
    if (marker == std::string::npos || keep_bytes <= kFooterReserve) return false;
    size_t first = 0, last = 0, total = 0;
    if (std::sscanf(content.c_str() + marker, "showed lines %zu-%zu of total %zu", &first, &last, &total) != 3)
        return false;
    const size_t footer_nl = content.rfind('\n', marker);
    if (footer_nl == std::string::npos) return false;
    const size_t body_budget = keep_bytes - kFooterReserve;
    if (footer_nl + 1 <= body_budget) return false;
    const size_t cut_nl = content.rfind('\n', Utf8Floor(content, body_budget));
    if (cut_nl == std::string::npos) return false;
    const size_t kept_lines = static_cast<size_t>(std::count(content.begin(), content.begin() + cut_nl + 1, '\n'));
    const size_t shown_last = first + kept_lines - 1;
    if (kept_lines == 0 || shown_last >= last) return false;
    const size_t footer_end = content.find('\n', marker);
    out = content.substr(0, cut_nl + 1) + "...[read truncated: context budget; showed lines " + std::to_string(first) +
          "-" + std::to_string(shown_last) + " of total " + std::to_string(total) +
          "; next offset=" + std::to_string(shown_last + 1) + "]" +
          (footer_end == std::string::npos ? std::string() : content.substr(footer_end));
    return true;
}

std::string CapToolResultHeadTail(const std::string& content, size_t content_tokens, size_t cap_tokens)
{
    if (cap_tokens == 0 || content_tokens <= cap_tokens || content.empty()) return content;
    const double keep_ratio = 0.95 * static_cast<double>(cap_tokens) / static_cast<double>(content_tokens);
    const size_t keep_bytes = static_cast<size_t>(static_cast<double>(content.size()) * keep_ratio);
    std::string paged_head;
    if (CapPagedReadHead(content, keep_bytes, paged_head)) return paged_head;
    const size_t head_target = static_cast<size_t>(static_cast<double>(keep_bytes) * kToolEntryHeadShare);
    const size_t tail_target = keep_bytes - head_target;
    size_t head_end = Utf8Floor(content, head_target);
    const size_t head_nl = content.rfind('\n', head_end);
    if (head_nl != std::string::npos && head_nl > head_end / 2) head_end = head_nl;
    size_t tail_begin = Utf8Floor(content, content.size() - tail_target);
    const size_t tail_nl = content.find('\n', tail_begin);
    if (tail_nl != std::string::npos && tail_nl + 1 < content.size() &&
        tail_nl - tail_begin < tail_target / 2) tail_begin = tail_nl + 1;
    if (tail_begin <= head_end) return content;
    const size_t error_budget = static_cast<size_t>(static_cast<double>(keep_bytes) * kToolEntryErrorShare);
    std::string errors;
    size_t pos = head_end;
    while (pos < tail_begin && errors.size() < error_budget) {
        size_t nl = content.find('\n', pos);
        if (nl == std::string::npos || nl > tail_begin) nl = tail_begin;
        const std::string line = content.substr(pos, nl - pos);
        if (IsErrorLine(line)) {
            const size_t room = error_budget - errors.size();
            errors += line.size() <= room ? line : line.substr(0, Utf8Floor(line, room));
            errors += '\n';
        }
        pos = nl + 1;
    }
    if (!errors.empty()) {
        const size_t shrink = std::min(errors.size(), head_end / 2);
        head_end = Utf8Floor(content, head_end - shrink);
        const std::string marker = OmittedMarker(content, head_end, tail_begin);
        return content.substr(0, head_end) + marker + kToolEntryErrorsHeader + errors + "\n" +
               content.substr(tail_begin);
    }
    return content.substr(0, head_end) + OmittedMarker(content, head_end, tail_begin) + content.substr(tail_begin);
}

constexpr size_t kMinSurvivorFloor = 4;
constexpr size_t kSurvivorCondenseMinShare = 48;
const char kRawIdxKey[] = "__genie_raw_idx";

struct EvictionState {
    const void* model = nullptr;
    size_t raw_size = 0;
    size_t boundary = 0;
    size_t prefix_hash = 0;
    std::vector<size_t> dropped;
};

std::mutex g_eviction_mutex;
std::unordered_map<std::string, EvictionState> g_eviction_states;

size_t HashRawPrefix(const json& raw, size_t len)
{
    size_t h = 1469598103934665603ULL;
    for (size_t i = 0; i < len && i < raw.size(); ++i) {
        h ^= std::hash<std::string>()(raw[i].dump()) + 0x9e3779b97f4a7c15ULL + (h << 6) + (h >> 2);
    }
    return h;
}

bool LookupEvictionState(const std::string& key, const void* model, const json& raw,
                         EvictionState& out, std::string& invalid_reason)
{
    std::lock_guard<std::mutex> lk(g_eviction_mutex);
    auto it = g_eviction_states.find(key);
    if (it == g_eviction_states.end()) {
        invalid_reason = "first_eviction";
        return false;
    }
    const EvictionState& st = it->second;
    if (st.model != model) {
        invalid_reason = "model_switch";
    } else if (raw.size() < st.raw_size || st.boundary > raw.size()) {
        invalid_reason = "message_shrink";
    } else if (HashRawPrefix(raw, st.boundary) != st.prefix_hash) {
        invalid_reason = "fingerprint_change";
    } else {
        out = st;
        return true;
    }
    g_eviction_states.erase(it);
    return false;
}

void StoreEvictionState(const std::string& key, EvictionState st)
{
    std::lock_guard<std::mutex> lk(g_eviction_mutex);
    if (st.dropped.empty()) {
        g_eviction_states.erase(key);
        return;
    }
    if (g_eviction_states.size() >= kMaxEvictionStates) g_eviction_states.clear();
    g_eviction_states[key] = std::move(st);
}

void StripRawIdx(json& messages)
{
    for (auto& m : messages) {
        if (m.is_object()) m.erase(kRawIdxKey);
    }
}


const char kRepeatStubPrefix[] = "[Note: this ";
const char kRepeatStubSuffix[] =
    " call returned exactly the same result as the earlier identical call, which is still shown above; "
    "calling it again will not return anything new. Use that result or change approach.";
const char kRepeatStubTailHeader[] = "\nIts last lines:\n";
constexpr size_t kRepeatStubTailLines = 3;
constexpr size_t kRepeatStubTailMaxBytes = 300;

std::string LastLines(const std::string& content, size_t n, size_t max_bytes)
{
    size_t end = content.find_last_not_of(" \t\r\n");
    if (end == std::string::npos) return "";
    ++end;
    size_t begin = end;
    for (size_t k = 0; k < n && begin > 0; ++k) {
        const size_t nl = content.rfind('\n', begin - 1);
        if (nl == std::string::npos) { begin = 0; break; }
        begin = nl;
    }
    if (begin < end && content[begin] == '\n') ++begin;
    if (end - begin > max_bytes) begin = Utf8Floor(content, end - max_bytes);
    while (begin < end && (static_cast<unsigned char>(content[begin]) & 0xC0) == 0x80) ++begin;
    return content.substr(begin, end - begin);
}
constexpr double kRepeatRetainRatio = 0.20;
constexpr double kRepeatRetainExceptionRatio = 0.40;

struct RepeatDecision {
    size_t content_hash = 0;
    bool stub = false;
};

struct RepeatState {
    const void* model = nullptr;
    std::unordered_map<size_t, RepeatDecision> decisions;
};

std::mutex g_repeat_mutex;
std::unordered_map<std::string, RepeatState> g_repeat_states;

void ApplyRepeatResultStubs(json& msg, const std::string& key, const void* model,
                             const std::unordered_set<size_t>& evicted_raw_idx,
                             bool eviction_state_unknown,
                             size_t cap_tokens,
                             size_t must_keep_budget_total,
                             const std::function<size_t(const std::string&)>& count_tokens_fn,
                             std::unordered_set<size_t>& protected_idx,
                             std::unordered_set<size_t>& must_keep_idx)
{
    protected_idx.clear();
    must_keep_idx.clear();
    size_t must_keep_used = 0;
    if (key.empty() || !msg.is_array()) return;
    std::lock_guard<std::mutex> lk(g_repeat_mutex);
    RepeatState& st = g_repeat_states[key];
    if (st.model != model) {
        st = RepeatState{};
        st.model = model;
    }
    for (const auto& kv : st.decisions) {
        if (kv.first >= msg.size()) { st.decisions.clear(); break; }
    }

    std::unordered_map<std::string, std::string> call_keys;
    std::unordered_map<std::string, std::string> call_names;
    std::string last_call_key;
    std::string last_call_name;
    struct KeyHistory { size_t full_idx = SIZE_MAX; size_t full_hash = 0; size_t repeat_count = 0; };
    std::unordered_map<std::string, KeyHistory> history;

    for (size_t i = 0; i < msg.size(); ++i) {
        json& m = msg[i];
        if (!m.is_object()) continue;
        const std::string role = m.value("role", std::string());
        if (role == "assistant") {
            last_call_key.clear();
            last_call_name.clear();
            if (m.contains("tool_calls") && m["tool_calls"].is_array()) {
                for (const auto& tc : m["tool_calls"]) {
                    const std::string ck = CanonicalToolCallKey(tc);
                    if (ck.empty()) continue;
                    const std::string nm = tc["function"].value("name", std::string());
                    if (tc.contains("id") && tc["id"].is_string()) {
                        call_keys[tc["id"].get<std::string>()] = ck;
                        call_names[tc["id"].get<std::string>()] = nm;
                    }
                    if (last_call_key.empty()) { last_call_key = ck; last_call_name = nm; }
                }
            }
            continue;
        }
        if (role != "tool" || !m.contains("content") || !m["content"].is_string()) continue;

        std::string ck = last_call_key;
        std::string nm = last_call_name;
        if (m.contains("tool_call_id") && m["tool_call_id"].is_string()) {
            auto it = call_keys.find(m["tool_call_id"].get<std::string>());
            if (it != call_keys.end()) { ck = it->second; nm = call_names[it->first]; }
        }
        if (ck.empty()) continue;

        const std::string content = m["content"].get<std::string>();
        const size_t h = std::hash<std::string>()(content);
        KeyHistory& kh = history[ck];
        const bool has_earlier_full = (kh.full_idx != SIZE_MAX && kh.full_hash == h);

        auto dit = st.decisions.find(i);
        if (dit != st.decisions.end() && dit->second.content_hash != h) {
            st.decisions.erase(dit);
            dit = st.decisions.end();
        }

        const bool alive = has_earlier_full && !eviction_state_unknown && !evicted_raw_idx.count(kh.full_idx);
        bool stub;
        bool keep_fail = false;
        const bool fresh = (dit == st.decisions.end());
        if (!fresh) {
            stub = dit->second.stub && has_earlier_full;
        } else {
            keep_fail = has_earlier_full && alive && TaskMemoBuilder::LooksLikeToolFailure(content);
            const bool want_stub = has_earlier_full && alive && !keep_fail;
            stub = false;
            if (want_stub && must_keep_budget_total > 0) {
                const size_t first_tok = cap_tokens > 0
                    ? std::min(count_tokens_fn(content), cap_tokens)
                    : count_tokens_fn(content);
                if (must_keep_used + first_tok <= must_keep_budget_total) {
                    stub = true;
                    must_keep_used += first_tok;
                    must_keep_idx.insert(kh.full_idx);
                }
            }
            st.decisions[i] = RepeatDecision{h, stub};
        }

        if (has_earlier_full) {
            ++kh.repeat_count;
            if (fresh) {
                My_Log{My_Log::Level::kInfo} << "[RepeatRead] key=" << std::hex << std::hash<std::string>()(ck)
                                              << std::dec << " idx=" << i << " count=" << kh.repeat_count
                                              << " alive=" << (alive ? 1 : 0) << " action="
                                              << (stub ? "stub" : (keep_fail ? "keep_fail" : "refill"))
                                              << std::endl;
            }
        }

        if (stub) {
            const std::string original = msg[kh.full_idx].value("content", std::string());
            std::string note = std::string(kRepeatStubPrefix) + nm + kRepeatStubSuffix;
            const std::string tail = LastLines(original, kRepeatStubTailLines, kRepeatStubTailMaxBytes);
            if (!tail.empty()) note += kRepeatStubTailHeader + tail;
            m["content"] = note + "]";
        } else {
            if (kh.full_hash != h) kh.repeat_count = 0;
            kh.full_idx = i;
            kh.full_hash = h;
        }
    }

    for (const auto& kv : history) {
        if (kv.second.repeat_count > 0 && kv.second.full_idx != SIZE_MAX) protected_idx.insert(kv.second.full_idx);
    }

    if (g_repeat_states.size() > kMaxEvictionStates) {
        RepeatState keep = std::move(st);
        g_repeat_states.clear();
        g_repeat_states[key] = std::move(keep);
    }
}

constexpr size_t kGrowthWindow = 4;
constexpr float kGrowthReserveRounds = 3.0f;
constexpr float kEvictionLowWaterFloorRatio = 0.4f;

struct GrowthState {
    const void* model = nullptr;
    size_t raw_size = 0;
    size_t last_tokens = 0;
    std::vector<size_t> growth;
};

std::mutex g_growth_mutex;
std::unordered_map<std::string, GrowthState> g_growth_states;

size_t ObserveRecentGrowth(const std::string& key, const void* model, size_t raw_size, size_t tokens)
{
    std::lock_guard<std::mutex> lk(g_growth_mutex);
    auto it = g_growth_states.find(key);
    if (it == g_growth_states.end()) return 0;
    GrowthState& st = it->second;
    if (st.model != model || raw_size < st.raw_size) {
        g_growth_states.erase(it);
        return 0;
    }
    std::vector<size_t> window = st.growth;
    if (raw_size > st.raw_size && tokens > st.last_tokens) window.push_back(tokens - st.last_tokens);
    if (window.size() > kGrowthWindow) window.erase(window.begin(), window.end() - kGrowthWindow);
    size_t mx = 0;
    for (size_t g : window) mx = std::max(mx, g);
    return mx;
}

void StoreRoundTokens(const std::string& key, const void* model, size_t raw_size,
                      size_t tokens_before_evict, size_t final_tokens)
{
    std::lock_guard<std::mutex> lk(g_growth_mutex);
    auto it = g_growth_states.find(key);
    if (it != g_growth_states.end() && (it->second.model != model || raw_size < it->second.raw_size)) {
        g_growth_states.erase(it);
        it = g_growth_states.end();
    }
    if (it == g_growth_states.end()) {
        if (g_growth_states.size() >= kMaxEvictionStates) g_growth_states.clear();
        GrowthState fresh;
        fresh.model = model;
        fresh.raw_size = raw_size;
        fresh.last_tokens = final_tokens;
        g_growth_states[key] = std::move(fresh);
        return;
    }
    GrowthState& st = it->second;
    if (raw_size > st.raw_size && tokens_before_evict > st.last_tokens) {
        st.growth.push_back(tokens_before_evict - st.last_tokens);
        if (st.growth.size() > kGrowthWindow) st.growth.erase(st.growth.begin());
    }
    st.raw_size = raw_size;
    st.last_tokens = final_tokens;
}

constexpr size_t kMaxTruncFloorEntries = 4096;
std::mutex g_trunc_floor_mutex;
std::unordered_map<size_t, size_t> g_trunc_floor_chars;

size_t HashTruncKey(const std::string& role, const std::string& original_content)
{
    size_t h = std::hash<std::string>()(role);
    const std::string prefix = original_content.substr(0, std::min<size_t>(256, original_content.size()));
    h ^= std::hash<std::string>()(prefix) + 0x9e3779b97f4a7c15ULL + (h << 6) + (h >> 2);
    h ^= std::hash<size_t>()(original_content.size()) + 0x9e3779b97f4a7c15ULL + (h << 6) + (h >> 2);
    return h;
}

size_t ClampToTruncFloor(size_t key, size_t target_chars)
{
    std::lock_guard<std::mutex> lk(g_trunc_floor_mutex);
    auto it = g_trunc_floor_chars.find(key);
    if (it != g_trunc_floor_chars.end() && it->second < target_chars) {
        return it->second;
    }
    return target_chars;
}

void UpdateTruncFloor(size_t key, size_t truncated_chars)
{
    std::lock_guard<std::mutex> lk(g_trunc_floor_mutex);
    auto it = g_trunc_floor_chars.find(key);
    if (it == g_trunc_floor_chars.end()) {
        if (g_trunc_floor_chars.size() >= kMaxTruncFloorEntries) g_trunc_floor_chars.clear();
        g_trunc_floor_chars[key] = truncated_chars;
    } else if (truncated_chars < it->second) {
        it->second = truncated_chars;
    }
}

const char kToolCallOpenTag[] = "<tool_call>";
const char kToolCallCloseTag[] = "</tool_call>";

bool HasToolCallEnvelope(const std::string& content)
{
    return content.find(kToolCallOpenTag) != std::string::npos &&
           content.find(kToolCallCloseTag) != std::string::npos;
}

bool ShrinkToolCallEnvelope(const std::string& content,
                            size_t target_chars,
                            const CondenseBudget& base_budget,
                            std::string& out)
{
    const size_t open_pos = content.find(kToolCallOpenTag);
    const size_t close_pos = content.rfind(kToolCallCloseTag);
    if (open_pos == std::string::npos || close_pos == std::string::npos) return false;
    const size_t body_begin = open_pos + (sizeof(kToolCallOpenTag) - 1);
    if (close_pos <= body_begin) return false;

    json parsed = json::parse(content.substr(body_begin, close_pos - body_begin), nullptr, false);
    if (parsed.is_discarded() || !parsed.is_object()) return false;
    if (!parsed.contains("name") || !parsed.contains("arguments")) return false;
    if (!parsed["arguments"].is_object()) return false;

    std::string largest_key;
    size_t largest_len = 0;
    for (auto it = parsed["arguments"].begin(); it != parsed["arguments"].end(); ++it) {
        if (!it.value().is_string()) continue;
        const size_t len = it.value().get<std::string>().length();
        if (len > largest_len) {
            largest_len = len;
            largest_key = it.key();
        }
    }
    if (largest_key.empty() || largest_len == 0) return false;

    const std::string original_value = parsed["arguments"][largest_key].get<std::string>();
    size_t value_budget = largest_len;
    std::string candidate;
    for (int attempt = 0; attempt < 8; ++attempt) {
        const size_t current_len = candidate.empty() ? content.length() : candidate.length();
        if (current_len <= target_chars) break;
        const size_t excess = current_len - target_chars;
        value_budget = (excess >= value_budget) ? (value_budget / 2) : (value_budget - excess);
        if (value_budget == 0) break;

        CondenseBudget value_cfg = base_budget;
        value_cfg.max_chars = value_budget;
        value_cfg.max_tokens = 0;
        CondenseResult condensed = ContentCondenser::Condense(
            original_value, ContentKind::kGenericText, value_cfg, nullptr);
        if (condensed.text.length() >= largest_len) break;

        json shrunk = parsed;
        shrunk["arguments"][largest_key] = condensed.text;
        candidate = std::string(kToolCallOpenTag) + "\n" + shrunk.dump() + "\n" + kToolCallCloseTag;
    }

    if (candidate.empty() || candidate.length() >= content.length()) return false;
    out = candidate;
    return true;
}

}  // namespace

void ClearPreFilterSessionStates()
{
    {
        std::lock_guard<std::mutex> lock(g_eviction_mutex);
        g_eviction_states.clear();
    }
    {
        std::lock_guard<std::mutex> lock(g_repeat_mutex);
        g_repeat_states.clear();
    }
    {
        std::lock_guard<std::mutex> lock(g_growth_mutex);
        g_growth_states.clear();
    }
    {
        std::lock_guard<std::mutex> lock(g_trunc_floor_mutex);
        g_trunc_floor_chars.clear();
    }
}

// ============================================================
// Constructor
// ============================================================

MessagePreFilter::MessagePreFilter(IModelConfig& model_config, ContextBase* context)
    : model_config_(model_config), context_override_(context)
{}

bool MessagePreFilter::PrefixCacheActive()
{
    if (context_override_) {
        return context_override_->UsesCrossRequestPrefixCache();
    }
    auto handle = model_config_.get_genie_model_handle().lock();
    return handle && handle->UsesCrossRequestPrefixCache();
}

// ============================================================
// CountTokens
// ============================================================

size_t MessagePreFilter::CountTokens(const std::string& text)
{
    if (context_override_) {
        return context_override_->TokenLength(text);
    }
    auto handle = model_config_.get_genie_model_handle().lock();
    if (handle) {
        return handle->TokenLength(text);
    }
    const auto& fid_cfg = model_config_.GetPromptOptimizationConfig().fidelity;
    double bytes_per_token = EstimateCjkAwareBytesPerToken(
        text, fid_cfg.cjk_bytes_per_token, fid_cfg.ascii_bytes_per_token);
    return static_cast<size_t>(static_cast<double>(text.length()) / bytes_per_token);
}

// ============================================================
// ShouldKeepFull
// ============================================================

bool MessagePreFilter::ShouldKeepFull(const GenieChatMessage& msg) const
{
    if (msg.role == "tool") {
        return true;
    }

    if (msg.role == "assistant") {
        if (msg.content.find("to=functions.") != std::string::npos) {
            return true;
        }

        if (msg.content.find("<tool_call>") != std::string::npos) {
            return true;
        }
    }

    return false;
}

// ============================================================
// CaptureDroppedMessages
// ============================================================

void MessagePreFilter::CaptureDroppedMessages(const json& original, const json& kept)
{
    auto message_content_for_memo = [](const json& m) -> std::string {
        std::string content = get_json_value(m, "content", BLANK_STRING);
        if (get_json_value(m, "role", BLANK_STRING) != "assistant" ||
            !m.contains("tool_calls") || !m["tool_calls"].is_array())
            return content;
        for (const auto& tc : m["tool_calls"]) {
            if (!tc.is_object() || !tc.contains("function") || !tc["function"].is_object())
                continue;
            const auto& fn = tc["function"];
            std::string name = (fn.contains("name") && fn["name"].is_string())
                ? fn["name"].get<std::string>() : std::string();
            if (name.empty())
                continue;
            std::string args;
            if (fn.contains("arguments") && !fn["arguments"].is_null()) {
                args = fn["arguments"].is_string() ? fn["arguments"].get<std::string>()
                                                   : fn["arguments"].dump();
            }
            if (args.empty())
                args = "{}";
            content += std::string(kToolCallOpenTag) + "\n{\"name\": \"" + name +
                       "\", \"arguments\": " + args + "}\n" + kToolCallCloseTag;
        }
        return content;
    };

    std::unordered_set<size_t> kept_raw;
    for (const auto& m : kept) {
        if (m.is_object() && m.contains(kRawIdxKey)) kept_raw.insert(m[kRawIdxKey].get<size_t>());
    }
    if (!kept_raw.empty()) {
        for (size_t i = 0; i < original.size(); ++i) {
            const std::string role = get_json_value(original[i], "role", BLANK_STRING);
            if (role == "system" || role == "developer" || kept_raw.count(i)) continue;
            prefilter_dropped_messages_.push_back({role, message_content_for_memo(original[i])});
        }
        return;
    }
    std::unordered_multiset<std::string> kept_signatures;
    for (const auto& m : kept) {
        std::string role = get_json_value(m, "role", BLANK_STRING);
        std::string content = message_content_for_memo(m);
        kept_signatures.insert(role + "\x01" + content);
    }
    for (const auto& m : original) {
        std::string role = get_json_value(m, "role", BLANK_STRING);
        if (role == "system" || role == "developer")
            continue;
        std::string content = message_content_for_memo(m);
        auto it = kept_signatures.find(role + "\x01" + content);
        if (it != kept_signatures.end()) {
            kept_signatures.erase(it);
            continue;
        }
        prefilter_dropped_messages_.push_back({role, content});
    }
}

// ============================================================
// FitMessagesToContext
// ============================================================

OptimizedMessages MessagePreFilter::FitMessagesToContext(
    const std::vector<GenieChatMessage>& all_messages,
    const std::string& system_prompt,
    size_t context_size,
    const MessageCompressionConfig& config,
    const nlohmann::ordered_json* raw_messages_for_memo,
    TaskMemoBuilder* memo_builder)
{
    OptimizedMessages result;
    result.success = false;
    result.dropped_count = 0;

    My_Log{My_Log::Level::kDebug} << "[FitMessagesToContext] Input: " << all_messages.size()
                                   << " messages, Context: " << context_size << " tokens" << std::endl;
    My_Log{My_Log::Level::kDebug} << "[FitMessagesToContext] Note: Message count, token pre-filtering, and content compression already done in PreFilterMessages()" << std::endl;

    // 步骤 1: 计算系统提示词的 tokens
    size_t system_tokens = CountTokens(system_prompt);

    // 检查系统提示词是否过大
    if (system_tokens > context_size) {
        result.success = false;
        result.error_message = "System prompt alone (" + std::to_string(system_tokens) +
                              " tokens) exceeds context size (" +
                              std::to_string(context_size) + " tokens). " +
                              "Please reduce system prompt or increase context size.";
        My_Log{My_Log::Level::kError} << result.error_message << std::endl;
        return result;
    }

    My_Log{My_Log::Level::kDebug} << "[FitMessagesToContext] Step 1: System tokens: " << system_tokens << std::endl;

    size_t available_tokens = context_size - system_tokens;

    // 步骤 2: 计算总 tokens
    MessageCategoryStats stats;
    stats.system_tokens = system_tokens;
    size_t total_tokens = system_tokens;

    for (const auto& msg : all_messages) {
        size_t msg_tokens = CountTokens(msg.content);
        total_tokens += msg_tokens;
        stats.total_tokens += msg_tokens;

        if (model_config_.getenablePromptDebug()) {
            if (msg.role == "user") {
                stats.user_count++;
                stats.user_tokens += msg_tokens;
            } else if (msg.role == "assistant") {
                stats.assistant_count++;
                stats.assistant_tokens += msg_tokens;
            } else if (msg.role == "tool") {
                stats.tool_count++;
                stats.tool_tokens += msg_tokens;
            }
        }
    }

    if (model_config_.getenablePromptDebug()) {
        My_Log{My_Log::Level::kDebug} << "[FitMessagesToContext] Step 2: Messages: " << stats.total_tokens
                                   << " tokens, Total: " << total_tokens
                                   << " tokens, Context: " << context_size << " tokens" << std::endl;
    }

    // 步骤 3: 如果仍然超出 contextSize，逐个丢弃旧消息
    // 保护策略：最后一条 user 消息及其之后的消息不可丢弃
    // assistant+tool 配对检测：丢弃 assistant 时，同时丢弃其后所有连续的 tool 消息
    // 两阶段丢弃策略：
    //   第一阶段：优先丢弃 recent_boundary 之前的旧消息
    //   第二阶段：若第一阶段仍不满足，再丢弃 recent_boundary 之后（但 last_user_index 之前）的消息
    std::vector<GenieChatMessage> final_messages = all_messages;
    std::vector<GenieChatMessage> dropped_for_memo;

    if (total_tokens > context_size) {
        My_Log{My_Log::Level::kDebug} << "[FitMessagesToContext] Step 3: Dropping old messages to fit context..." << std::endl;

        const size_t evict_target_size = PrefixCacheActive()
            ? static_cast<size_t>(static_cast<float>(context_size) * kEvictionLowWaterRatio)
            : context_size;

        // 找到最后一条 user 消息的位置
        int last_user_index = -1;
        for (int i = (int)final_messages.size() - 1; i >= 0; i--) {
            if (final_messages[i].role == "user") {
                last_user_index = i;
                break;
            }
        }

        int last_tool_index = -1;
        for (int i = (int)final_messages.size() - 1; i >= 0; i--) {
            if (final_messages[i].role == "tool") {
                last_tool_index = i;
                break;
            }
        }

        auto compute_recent_boundary = [&]() -> int {
            if (last_user_index < 0) return 0;
            int boundary = last_user_index - (int)config.keep_recent_full;
            return (boundary > 0) ? boundary : 0;
        };

        auto is_fit_protected = [&](int idx) -> bool {
            if (last_user_index >= 0 && idx >= last_user_index) return true;
            if (last_tool_index >= 0 && idx >= last_tool_index) return true;
            return false;
        };

        auto try_drop_oldest_before = [&](int drop_before) -> bool {
            if (final_messages.empty() || drop_before <= 0) return false;

            std::string role_to_drop = final_messages[0].role;

            if (role_to_drop == "assistant") {
                size_t tool_count = 0;
                for (size_t k = 1; k < final_messages.size(); k++) {
                    if (final_messages[k].role == "tool") tool_count++;
                    else break;
                }
                size_t drop_count = 1 + tool_count;
                if ((int)drop_count > drop_before) return false;
                if (drop_count == 0) return false;
                if (last_tool_index >= 0 && last_tool_index < (int)drop_count) return false;

                size_t dropped_tokens = 0;
                for (size_t k = 0; k < drop_count; k++)
                    dropped_tokens += CountTokens(final_messages[k].content);

                for (size_t k = 0; k < drop_count; k++)
                    dropped_for_memo.push_back(final_messages[k]);
                final_messages.erase(final_messages.begin(), final_messages.begin() + drop_count);
                total_tokens -= dropped_tokens;
                result.dropped_count += drop_count;

                if (last_user_index >= (int)drop_count) last_user_index -= (int)drop_count;
                else last_user_index = -1;
                if (last_tool_index >= (int)drop_count) last_tool_index -= (int)drop_count;
                else last_tool_index = -1;

                My_Log{My_Log::Level::kDebug}
                    << "[FitMessagesToContext]   Dropped assistant+" << tool_count
                    << " tool(s) (saved " << dropped_tokens
                    << " tokens, remaining: " << total_tokens << ")" << std::endl;
                return true;
            }

            if (role_to_drop == "tool") {
                if (last_tool_index == 0) return false;
                My_Log{My_Log::Level::kWarning}
                    << "[FitMessagesToContext] Found orphaned tool message at position 0, dropping it" << std::endl;
            }

            size_t dropped_tokens = CountTokens(final_messages[0].content);
            dropped_for_memo.push_back(final_messages[0]);
            final_messages.erase(final_messages.begin());
            total_tokens -= dropped_tokens;
            result.dropped_count++;

            if (last_user_index >= 1) last_user_index -= 1;
            else last_user_index = -1;
            if (last_tool_index >= 1) last_tool_index -= 1;
            else last_tool_index = -1;

            My_Log{My_Log::Level::kDebug}
                << "[FitMessagesToContext]   Dropped oldest message (role: " << role_to_drop
                << ", saved " << dropped_tokens
                << " tokens, remaining: " << total_tokens << ")" << std::endl;
            return true;
        };

        My_Log{My_Log::Level::kDebug} << "[FitMessagesToContext] Step 3a: Dropping old messages (before recent_boundary)..." << std::endl;
        while (total_tokens > evict_target_size) {
            if (final_messages.empty()) break;
            int recent_boundary = compute_recent_boundary();
            if (recent_boundary <= 0) break;
            if (!try_drop_oldest_before(recent_boundary)) break;
        }

        if (total_tokens > evict_target_size) {
            My_Log{My_Log::Level::kDebug} << "[FitMessagesToContext] Step 3b: Dropping recent messages (before last_user_index)..." << std::endl;
        }
        while (total_tokens > evict_target_size) {
            if (final_messages.empty()) break;
            if (is_fit_protected(0)) {
                My_Log{My_Log::Level::kWarning}
                    << "[FitMessagesToContext] All remaining messages are protected (last_user_index="
                    << last_user_index << ", last_tool_index=" << last_tool_index
                    << "), cannot drop further" << std::endl;
                break;
            }
            {
                int drop_before = (int)final_messages.size();
                if (last_user_index >= 0) drop_before = std::min(drop_before, last_user_index);
                if (last_tool_index >= 0) drop_before = std::min(drop_before, last_tool_index);
                if (!try_drop_oldest_before(drop_before)) break;
            }
        }

        if (total_tokens > context_size) {
            const auto& et_cfg = model_config_.GetPromptOptimizationConfig().emergency_truncation;
            const auto& fid_cfg = model_config_.GetPromptOptimizationConfig().fidelity;
            bool phase4_applied = false;

            if (et_cfg.enabled && et_cfg.target_largest_tool_messages) {
                std::vector<int> cand_indices;
                for (int i = 0; i < (int)final_messages.size(); i++) {
                    if (final_messages[i].role == "tool" ||
                        (final_messages[i].role == "assistant" &&
                         HasToolCallEnvelope(final_messages[i].content))) {
                        cand_indices.push_back(i);
                    }
                }

                if (!cand_indices.empty()) {
                    std::sort(cand_indices.begin(), cand_indices.end(), [&](int a, int b) {
                        return CountTokens(final_messages[a].content) > CountTokens(final_messages[b].content);
                    });

                    size_t overflow_tokens = total_tokens - context_size;
                    size_t tokens_to_free = overflow_tokens + static_cast<size_t>(et_cfg.safety_margin_tokens);
                    size_t needed = tokens_to_free;
                    size_t freed_total = 0;
                    size_t largest_tokens = CountTokens(final_messages[cand_indices[0]].content);
                    int targets_used = 0;
                    int tool_targets = 0;
                    int asst_targets = 0;
                    size_t old_total = total_tokens;
                    std::vector<std::pair<int, std::string>> backups;

                    My_Log{My_Log::Level::kWarning}
                        << "[FitMessagesToContext] Phase 4: Emergency truncation check: "
                        << "overflow=" << overflow_tokens << " tok"
                        << ", safety_margin=" << et_cfg.safety_margin_tokens << " tok"
                        << ", tokens_to_free=" << tokens_to_free << " tok"
                        << ", candidates=" << cand_indices.size()
                        << ", largest=" << largest_tokens << " tok"
                        << " (max_ratio=" << et_cfg.max_truncation_ratio << ")" << std::endl;

                    for (int idx : cand_indices) {
                        if (needed == 0) break;
                        size_t msg_tokens = CountTokens(final_messages[idx].content);
                        if (msg_tokens == 0) continue;
                        size_t max_cut_allowed = static_cast<size_t>(
                            static_cast<float>(msg_tokens) * et_cfg.max_truncation_ratio);
                        size_t cut_amount = std::min(needed, max_cut_allowed);
                        if (cut_amount == 0) continue;

                        const std::string original_content = final_messages[idx].content;
                        const bool is_envelope = (final_messages[idx].role == "assistant");
                        size_t target_tokens = (msg_tokens > cut_amount) ? (msg_tokens - cut_amount) : 0;
                        {
                            const size_t floor_tokens = static_cast<size_t>(
                                static_cast<float>(msg_tokens) * (1.0f - et_cfg.max_truncation_ratio));
                            size_t step = msg_tokens;
                            while (step > target_tokens && step > floor_tokens) {
                                step = step * 3 / 4;
                            }
                            target_tokens = std::max(step, floor_tokens);
                        }
                        size_t target_chars = (original_content.length() > 0 && msg_tokens > 0)
                            ? static_cast<size_t>(
                                static_cast<float>(original_content.length()) *
                                static_cast<float>(target_tokens) /
                                static_cast<float>(msg_tokens) * 0.9f)
                            : 0;
                        const size_t trunc_key = HashTruncKey(final_messages[idx].role, original_content);
                        target_chars = ClampToTruncFloor(trunc_key, target_chars);
                        if (target_chars == 0 || target_chars >= original_content.length()) continue;

                        CondenseBudget emergency_budget;
                        emergency_budget.max_chars = target_chars;
                        emergency_budget.max_tokens = target_tokens;
                        emergency_budget.tail_ratio = fid_cfg.preserve_tail ? fid_cfg.tail_ratio : 0.0;
                        emergency_budget.extract_high_signal = fid_cfg.extract_high_signal;
                        emergency_budget.json_head_items = fid_cfg.json_head_items;
                        emergency_budget.json_tail_items = fid_cfg.json_tail_items;
                        emergency_budget.max_token_probe = fid_cfg.max_token_probe_per_message;
                        emergency_budget.cjk_bytes_per_token = fid_cfg.cjk_bytes_per_token;
                        emergency_budget.ascii_bytes_per_token = fid_cfg.ascii_bytes_per_token;

                        std::string truncated;
                        if (is_envelope) {
                            if (!ShrinkToolCallEnvelope(original_content, target_chars,
                                                        emergency_budget, truncated)) {
                                continue;
                            }
                        } else {
                            std::function<size_t(const std::string&)> emergency_token_len =
                                [this](const std::string& text) { return CountTokens(text); };
                            CondenseResult condensed = ContentCondenser::Condense(
                                original_content, ContentKind::kToolResponse, emergency_budget, &emergency_token_len);
                            truncated = condensed.text;
                        }

                        backups.emplace_back(idx, original_content);
                        final_messages[idx].content = truncated;
                        UpdateTruncFloor(trunc_key, truncated.length());

                        size_t new_tokens = CountTokens(truncated);
                        size_t actually_freed = (msg_tokens > new_tokens) ? (msg_tokens - new_tokens) : 0;
                        total_tokens = total_tokens - msg_tokens + new_tokens;
                        freed_total += actually_freed;
                        needed = (needed > actually_freed) ? (needed - actually_freed) : 0;
                        targets_used++;
                        if (is_envelope) asst_targets++; else tool_targets++;

                        My_Log{My_Log::Level::kWarning}
                            << "[FitMessagesToContext] Phase 4: truncated "
                            << final_messages[idx].role << " msg[" << idx << "] "
                            << original_content.length() << " chars/" << msg_tokens << " tok"
                            << " -> " << truncated.length() << " chars/" << new_tokens << " tok" << std::endl;
                    }

                    if (total_tokens <= context_size) {
                        phase4_applied = true;
                        My_Log{My_Log::Level::kWarning}
                            << "[FitMessagesToContext] Phase 4: Emergency truncation succeeded "
                            << "(targets=" << targets_used
                            << "(tool=" << tool_targets << ", asst=" << asst_targets << ")"
                            << ", freed=" << freed_total
                            << " tok, largest=" << largest_tokens
                            << " tok), context overflow resolved" << std::endl;
                    } else {
                        for (auto& b : backups) {
                            final_messages[b.first].content = b.second;
                        }
                        total_tokens = old_total;
                        My_Log{My_Log::Level::kWarning}
                            << "[FitMessagesToContext] Phase 4: Emergency truncation insufficient across "
                            << targets_used << " target(s) (still " << total_tokens << " > " << context_size
                            << "), reverting and triggering local_input_overflow" << std::endl;
                    }
                } else {
                    My_Log{My_Log::Level::kWarning}
                        << "[FitMessagesToContext] Phase 4: No truncatable message found in protected set, "
                        << "skipping emergency truncation" << std::endl;
                }
            } else if (et_cfg.enabled) {
                int last_tool_idx = -1;
                for (int i = (int)final_messages.size() - 1; i >= 0; i--) {
                    if (final_messages[i].role == "tool") {
                        last_tool_idx = i;
                        break;
                    }
                }

                if (last_tool_idx >= 0) {
                    size_t last_tool_tokens = CountTokens(final_messages[last_tool_idx].content);
                    size_t overflow_tokens = total_tokens - context_size;
                    size_t tokens_to_free = overflow_tokens + static_cast<size_t>(et_cfg.safety_margin_tokens);

                    float truncation_ratio = (last_tool_tokens > 0)
                        ? static_cast<float>(tokens_to_free) / static_cast<float>(last_tool_tokens)
                        : 1.0f;

                    My_Log{My_Log::Level::kWarning}
                        << "[FitMessagesToContext] Phase 4: Emergency truncation check: "
                        << "overflow=" << overflow_tokens << " tok"
                        << ", safety_margin=" << et_cfg.safety_margin_tokens << " tok"
                        << ", tokens_to_free=" << tokens_to_free << " tok"
                        << ", last_tool_tokens=" << last_tool_tokens << " tok"
                        << ", truncation_ratio=" << truncation_ratio
                        << " (max=" << et_cfg.max_truncation_ratio << ")" << std::endl;

                    if (truncation_ratio > et_cfg.max_truncation_ratio) {
                        My_Log{My_Log::Level::kWarning}
                            << "[FitMessagesToContext] Phase 4: Truncation ratio " << truncation_ratio
                            << " exceeds max_truncation_ratio " << et_cfg.max_truncation_ratio
                            << ", skipping emergency truncation (will trigger local_input_overflow)" << std::endl;
                    } else {
                        const std::string& tool_content = final_messages[last_tool_idx].content;
                        size_t target_tokens = (last_tool_tokens > tokens_to_free)
                            ? (last_tool_tokens - tokens_to_free)
                            : 0;
                        size_t target_chars = (tool_content.length() > 0 && last_tool_tokens > 0)
                            ? static_cast<size_t>(
                                static_cast<float>(tool_content.length()) *
                                static_cast<float>(target_tokens) /
                                static_cast<float>(last_tool_tokens) * 0.9f)
                            : 0;

                        if (target_chars > 0 && target_chars < tool_content.length()) {
                            CondenseBudget emergency_budget;
                            emergency_budget.max_chars = target_chars;
                            emergency_budget.max_tokens = target_tokens;
                            emergency_budget.tail_ratio = fid_cfg.preserve_tail ? fid_cfg.tail_ratio : 0.0;
                            emergency_budget.extract_high_signal = fid_cfg.extract_high_signal;
                            emergency_budget.json_head_items = fid_cfg.json_head_items;
                            emergency_budget.json_tail_items = fid_cfg.json_tail_items;
                            emergency_budget.max_token_probe = fid_cfg.max_token_probe_per_message;
                            emergency_budget.cjk_bytes_per_token = fid_cfg.cjk_bytes_per_token;
                            emergency_budget.ascii_bytes_per_token = fid_cfg.ascii_bytes_per_token;
                            std::function<size_t(const std::string&)> emergency_token_len =
                                [this](const std::string& text) { return CountTokens(text); };
                            CondenseResult condensed = ContentCondenser::Condense(
                                tool_content, ContentKind::kToolResponse, emergency_budget, &emergency_token_len);
                            std::string truncated = condensed.text;
                            final_messages[last_tool_idx].content = truncated;

                            size_t new_tool_tokens = CountTokens(truncated);
                            size_t old_total = total_tokens;
                            total_tokens = total_tokens - last_tool_tokens + new_tool_tokens;

                            My_Log{My_Log::Level::kWarning}
                                << "[FitMessagesToContext] Phase 4: Emergency truncation applied: "
                                << "tool msg[" << last_tool_idx << "] "
                                << tool_content.length() << " chars/" << last_tool_tokens << " tok"
                                << " -> " << truncated.length() << " chars/" << new_tool_tokens << " tok"
                                << ", total_tokens: " << old_total << " -> " << total_tokens
                                << " (context_size=" << context_size << ")" << std::endl;

                            if (total_tokens <= context_size) {
                                phase4_applied = true;
                                My_Log{My_Log::Level::kWarning}
                                    << "[FitMessagesToContext] Phase 4: Emergency truncation succeeded, "
                                    << "context overflow resolved" << std::endl;
                            } else {
                                final_messages[last_tool_idx].content = tool_content;
                                total_tokens = old_total;
                                My_Log{My_Log::Level::kWarning}
                                    << "[FitMessagesToContext] Phase 4: Emergency truncation insufficient "
                                    << "(still " << total_tokens << " > " << context_size
                                    << "), reverting and triggering local_input_overflow" << std::endl;
                            }
                        } else {
                            My_Log{My_Log::Level::kWarning}
                                << "[FitMessagesToContext] Phase 4: target_chars=" << target_chars
                                << " invalid (tool_content.length=" << tool_content.length()
                                << "), skipping emergency truncation" << std::endl;
                        }
                    }
                } else {
                    My_Log{My_Log::Level::kWarning}
                        << "[FitMessagesToContext] Phase 4: No tool message found in protected set, "
                        << "skipping emergency truncation" << std::endl;
                }
            }

            result.emergency_truncated = phase4_applied;

            // Phase 4 未能解决溢出，进入错误流程
            if (!phase4_applied) {
            result.success = false;
            result.error_message = "Protected messages (last user message and all subsequent messages) "
                                  "exceed context size (" + std::to_string(context_size) +
                                  " tokens). Current total: " + std::to_string(total_tokens) +
                                  " tokens. Cannot compress further.";
            My_Log{My_Log::Level::kError} << result.error_message << std::endl;

            My_Log{My_Log::Level::kError} << "[FitMessagesToContext] Message details (oldest to newest):" << std::endl;
            size_t accumulated_tokens = 0;
            for (size_t i = 0; i < final_messages.size(); ++i) {
                const auto& msg = final_messages[i];
                size_t msg_tokens = CountTokens(msg.content);
                accumulated_tokens += msg_tokens;

                std::string content_preview = msg.content;
                if (content_preview.length() > 200) {
                    content_preview = content_preview.substr(0, 200) + "...";
                }
                std::replace(content_preview.begin(), content_preview.end(), '\n', ' ');
                std::replace(content_preview.begin(), content_preview.end(), '\r', ' ');

                My_Log{My_Log::Level::kError}
                    << "  [" << (i + 1) << "/" << final_messages.size() << "] "
                    << "Role: " << msg.role << ", "
                    << "Tokens: " << msg_tokens << ", "
                    << "Length: " << msg.content.length() << ", "
                    << "Content: \"" << content_preview << "\"" << std::endl;
            }
            My_Log{My_Log::Level::kError} << "[FitMessagesToContext] Total messages tokens: " << accumulated_tokens << std::endl;
            My_Log{My_Log::Level::kError} << "[FitMessagesToContext] System tokens: " << system_tokens << std::endl;
            My_Log{My_Log::Level::kError} << "[FitMessagesToContext] Total tokens (system + messages): " << (system_tokens + accumulated_tokens) << std::endl;
            My_Log{My_Log::Level::kError} << "[FitMessagesToContext] Context size limit: " << context_size << std::endl;
            My_Log{My_Log::Level::kError} << "[FitMessagesToContext] Exceeded by: " << (system_tokens + accumulated_tokens - context_size) << " tokens" << std::endl;

            return result;
            } // if (!phase4_applied)
        }
    } else {
        My_Log{My_Log::Level::kDebug} << "[FitMessagesToContext] Step 3: Total tokens within context size" << std::endl;
    }

    if (memo_builder && raw_messages_for_memo) {
        std::vector<GenieChatMessage> all_dropped_for_memo = prefilter_dropped_messages_;
        all_dropped_for_memo.insert(all_dropped_for_memo.end(), dropped_for_memo.begin(), dropped_for_memo.end());
        auto memo_result = memo_builder->Update(*raw_messages_for_memo, all_dropped_for_memo);
        result.memo_active = memo_result.active;
        result.memo_confidence = memo_result.confidence;
        result.memo_refresh_count = memo_result.refresh_count;
        result.memo_pages_total = memo_result.pages_total;
    }

    const auto& fidelity_cfg_drop = model_config_.GetPromptOptimizationConfig().fidelity;
    const size_t total_dropped_all_phases = prefilter_stats_.total_dropped + result.dropped_count;
    if (total_tokens > context_size ||
        (total_dropped_all_phases > 0 && fidelity_cfg_drop.drop_placeholder && !final_messages.empty() &&
         context_size - total_tokens < 20)) {
        result.success = false;
        result.error_message = "Context size exceeded even after dropping all history. "
                              "System prompt (" + std::to_string(system_tokens) +
                              " tokens) + remaining messages exceed context size (" +
                              std::to_string(context_size) + " tokens).";
        My_Log{My_Log::Level::kError} << result.error_message << std::endl;
        return result;
    }

    if (total_dropped_all_phases > 0 && fidelity_cfg_drop.drop_placeholder && !final_messages.empty()) {
        static const std::string placeholder = "[earlier message(s) omitted]\n";

        total_tokens += CountTokens(placeholder);
        final_messages[0].content = placeholder + final_messages[0].content;
        My_Log{My_Log::Level::kDebug} << "[FitMessagesToContext] Inserted drop placeholder for "
                                       << total_dropped_all_phases << " dropped message(s) (prefilter="
                                       << prefilter_stats_.total_dropped << " + fit=" << result.dropped_count
                                       << ")" << std::endl;
    }

    // 成功
    result.messages = final_messages;
    result.total_tokens = total_tokens;
    result.success = true;

    My_Log{My_Log::Level::kInfo} << "[FitMessagesToContext] Success - Messages: " << final_messages.size()
                                  << ", Tokens: " << total_tokens
                                  << ", Available: " << (static_cast<long long>(context_size) -
                                                         static_cast<long long>(total_tokens))
                                  << ", Dropped: " << result.dropped_count << std::endl;

    return result;
}

// ============================================================
// IsToolRelatedMessage
// ============================================================

bool MessagePreFilter::IsToolRelatedMessage(const json& element, const std::string& role)
{
    if (role == "tool") {
        return true;
    }

    if (role == "assistant") {
        // 优先检查 tool_calls 字段（OpenAI 标准格式）
        if (element.contains("tool_calls") && !element["tool_calls"].is_null() &&
            element["tool_calls"].is_array() && !element["tool_calls"].empty()) {
            return true;
        }

        // 检查 content 中的工具调用标记（仅当 content 存在且为字符串时）
        if (element.contains("content") && element["content"].is_string()) {
            std::string content = element["content"].get<std::string>();
            if (!content.empty() &&
                (content.find("to=functions.") != std::string::npos ||
                 content.find("<tool_call>") != std::string::npos)) {
                return true;
            }
        }
    }

    return false;
}

// ============================================================
// RemoveToolResponseRedundancy
// ============================================================

// 移除 Tool 返回内容中的冗余安全警告
// 单次扫描 + 输出缓冲区策略（O(n)）：
//   1. 遇到 "SECURITY NOTICE:" 时，跳过直到 "<<<EXTERNAL_UNTRUSTED_CONTENT>>>" 结束
//   2. 遇到独立的 "<<<EXTERNAL_UNTRUSTED_CONTENT>>>" 时，直接跳过
//   3. 遇到 "<<<END_EXTERNAL_UNTRUSTED_CONTENT>>>" 时，直接跳过
//   4. 其余字符原样追加到输出缓冲区
//   5. 最后对输出做连续换行符压缩和首尾空白清理
std::string MessagePreFilter::RemoveToolResponseRedundancy(const std::string& content)
{
    static const std::string security_notice_start = "SECURITY NOTICE:";
    static const std::string start_marker          = "<<<EXTERNAL_UNTRUSTED_CONTENT>>>";
    static const std::string end_marker            = "<<<END_EXTERNAL_UNTRUSTED_CONTENT>>>";

    // 快速路径：若内容中不含任何标记，直接返回原字符串，避免不必要的内存分配
    if (content.find(security_notice_start) == std::string::npos &&
        content.find(start_marker)          == std::string::npos &&
        content.find(end_marker)            == std::string::npos)
    {
        return content;
    }

    std::string result;
    result.reserve(content.size());

    size_t pos = 0;
    const size_t len = content.size();

    while (pos < len) {
        // ── 检查 "SECURITY NOTICE:" ──────────────────────────────────────
        if (content.compare(pos, security_notice_start.size(), security_notice_start) == 0) {
            // 向后查找对应的 start_marker
            size_t sm_pos = content.find(start_marker, pos + security_notice_start.size());
            if (sm_pos != std::string::npos) {
                // 跳过从 "SECURITY NOTICE:" 到 start_marker 末尾的整块内容
                pos = sm_pos + start_marker.size();
            } else {
                // 只有头没有尾，只跳过 "SECURITY NOTICE:" 本身
                pos += security_notice_start.size();
            }
            continue;
        }

        // ── 检查 "<<<EXTERNAL_UNTRUSTED_CONTENT>>>" ──────────────────────
        if (content.compare(pos, start_marker.size(), start_marker) == 0) {
            pos += start_marker.size();
            continue;
        }

        // ── 检查 "<<<END_EXTERNAL_UNTRUSTED_CONTENT>>>" ──────────────────
        if (content.compare(pos, end_marker.size(), end_marker) == 0) {
            pos += end_marker.size();
            continue;
        }

        // ── 普通字符，原样追加 ───────────────────────────────────────────
        result += content[pos];
        ++pos;
    }

    // 清理多余的空白行（3个或更多连续换行符压缩为2个）
    {
        std::string cleaned;
        cleaned.reserve(result.size());
        size_t i = 0;
        while (i < result.size()) {
            if (result[i] == '\n') {
                size_t nl_count = 0;
                while (i < result.size() && result[i] == '\n') {
                    ++nl_count;
                    ++i;
                }
                cleaned += '\n';
                if (nl_count >= 2) cleaned += '\n';
            } else {
                cleaned += result[i];
                ++i;
            }
        }
        result = std::move(cleaned);
    }

    // 清理首尾空白
    return SecurityUtils::TrimWhitespace(result);
}

std::string MessagePreFilter::RemoveUntrustedMetadataBlocks(const std::string& content)
{
    if (content.find("Sender (untrusted metadata):") == std::string::npos &&
        content.find("<environment_details>") == std::string::npos) {
        return content;
    }

    std::string result = content;

    static const std::regex sender_block_pattern(
        R"((?:^|\r?\n)[ \t]*Sender \(untrusted metadata\):[ \t]*(?:\r?\n|$)(?:[ \t]*```json[ \t]*(?:\r?\n|$)[\s\S]*?[ \t]*```[ \t]*)?)",
        std::regex::ECMAScript);
    result = std::regex_replace(result, sender_block_pattern, "\n");

    static const std::regex environment_block_pattern(
        R"((?:^|\r?\n)[ \t]*<environment_details>[\s\S]*?</environment_details>[ \t]*)",
        std::regex::ECMAScript);
    result = std::regex_replace(result, environment_block_pattern, "\n");

    result = std::regex_replace(result, std::regex(R"(\n{3,})"), "\n\n");

    return SecurityUtils::TrimWhitespace(result);
}

std::string MessagePreFilter::CleanMessageContent(const std::string& content)
{
    std::string cleaned = RemoveUntrustedMetadataBlocks(content);

    static const std::regex timestamp_prefix_pattern(
        R"(^[ \t]*\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+GMT[+-]\d+\][ \t]*)",
        std::regex::ECMAScript);
    cleaned = std::regex_replace(cleaned, timestamp_prefix_pattern, "");

    return SecurityUtils::TrimWhitespace(cleaned);
}

// ============================================================
// SmartSelectMessages
// ============================================================

// 智能选择消息，确保：
// 1. 至少保留一条 user 消息
// 2. tool 消息前有对应的 assistant 消息
// 3. 保持消息的原始顺序
json MessagePreFilter::SmartSelectMessages(const json& msg, size_t max_messages)
{
    if (model_config_.getenablePromptDebug()) {
        My_Log{My_Log::Level::kDebug} << "[SmartSelect] Input: " << msg.size()
                                       << " messages, max: " << max_messages << std::endl;
    }

    // 步骤 1: 简单保留最新的消息（保持原始顺序）
    json selected = json::array();
    // selected[i] 对应 msg[selected_orig_indices[i]]
    std::vector<size_t> selected_orig_indices;

    // 保留 system 消息
    bool has_system_msg = false;
    for (size_t i = 0; i < msg.size(); i++) {
        if (msg[i]["role"] == "system") {
            selected.push_back(msg[i]);
            selected_orig_indices.push_back(i);
            has_system_msg = true;
            break;
        }
    }

    // 计算起始索引（保留最新的非 system 消息）
    // 若有 system 消息，system 消息占用 1 个槽位，非 system 消息最多保留 max_messages-1 条；
    // 若无 system 消息，所有槽位都可用于非 system 消息，最多保留 max_messages 条
    size_t keep_count = has_system_msg
        ? (max_messages > 1 ? max_messages - 1 : 0)
        : max_messages;

    // 先收集非 system 消息的原始索引
    std::vector<size_t> non_system_indices;
    for (size_t i = 0; i < msg.size(); i++) {
        if (msg[i]["role"] != "system") {
            non_system_indices.push_back(i);
        }
    }

    // 基于非 system 消息数量计算跳过数量
    size_t skip_count = (non_system_indices.size() > keep_count)
                        ? (non_system_indices.size() - keep_count) : 0;

    // 保留最新的 keep_count 条非 system 消息（同时记录原始索引）
    for (size_t k = skip_count; k < non_system_indices.size(); k++) {
        selected.push_back(msg[non_system_indices[k]]);
        selected_orig_indices.push_back(non_system_indices[k]);
    }

    // 步骤 2: 检查是否有 user 消息
    bool has_user = false;

    // 在选中的消息中查找 user 消息
    for (size_t i = 0; i < selected.size(); i++) {
        if (selected[i]["role"] == "user") {
            has_user = true;
            break;
        }
    }

    // 如果没有 user 消息，往前找最新的一条
    if (!has_user) {
        int last_user_index = -1;
        for (int i = msg.size() - 1; i >= 0; i--) {
            if (msg[i]["role"] == "user") {
                last_user_index = i;
                break;
            }
        }

        if (last_user_index >= 0) {
            // 有 system 消息且 selected 非空时插入到位置 1，否则插入到位置 0（头部）
            size_t insert_pos = (has_system_msg && !selected.empty()) ? 1 : 0;
            selected.insert(selected.begin() + insert_pos, msg[last_user_index]);
            selected_orig_indices.insert(selected_orig_indices.begin() + insert_pos, (size_t)last_user_index);
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kWarning} << "[SmartSelect] Force added last user message from index "
                                                 << last_user_index << std::endl;
            }
        }
    }

    // 步骤 3: 确保 tool 消息前有对应的 assistant 消息
    // 构建 tool → assistant 依赖关系
    std::map<size_t, int> tool_needs_assistant;  // selected中的tool索引 → 原始msg中的assistant索引

    for (size_t i = 0; i < selected.size(); i++) {
        if (selected[i]["role"] == "tool") {
            // 在 selected 中向前查找 assistant
            bool found_in_selected = false;
            for (int j = i - 1; j >= 0; j--) {
                auto role = get_json_value(selected[j], "role", BLANK_STRING);
                if (role == "assistant" && IsToolRelatedMessage(selected[j], role)) {
                    found_in_selected = true;
                    break;
                }
            }

            if (!found_in_selected) {
                size_t orig_i = selected_orig_indices[i];
                // 向前查找 assistant
                for (int j = (int)orig_i - 1; j >= 0; j--) {
                    auto role = get_json_value(msg[j], "role", BLANK_STRING);
                    if (role == "assistant" && IsToolRelatedMessage(msg[j], role)) {
                        tool_needs_assistant[i] = j;
                        if (model_config_.getenablePromptDebug()) {
                            My_Log{My_Log::Level::kDebug} << "[SmartSelect] Tool at selected[" << i
                                                           << "] needs assistant from msg[" << j << "]" << std::endl;
                        }
                        break;
                    }
                }
            }
        }
    }

    // 插入缺失的 assistant 消息（保持顺序）
    // std::map 按 key（tool_pos）升序迭代，保证从前往后处理，offset 单调递增
    size_t insert_offset = 0;
    for (const auto& pair : tool_needs_assistant) {
        size_t tool_pos = pair.first + insert_offset;
        int assistant_orig_index = pair.second;

        // 在 tool_pos 之前插入 assistant
        selected.insert(selected.begin() + tool_pos, msg[assistant_orig_index]);
        selected_orig_indices.insert(selected_orig_indices.begin() + tool_pos,
                                     (size_t)assistant_orig_index);
        insert_offset++;  // 每插入一条消息，后续索引 +1
        if (model_config_.getenablePromptDebug()) {
            My_Log{My_Log::Level::kWarning} << "[SmartSelect] Inserted missing assistant before tool at position "
                                             << tool_pos << " (offset=" << insert_offset << ")" << std::endl;
        }
    }

    // 步骤 3 补充 assistant 消息后，selected 可能超过 max_messages 约束
    // 若超出约束，从最旧的非保护消息中删除消息，直到满足约束
    // 保护规则：system、最后一条 user、最后一条 tool、最后一个 assistant tool_call、最后一条 assistant final 不可删除

    // 一次性扫描 selected，预计算各类消息的最后索引
    auto compute_last_indices = [&]() -> std::array<size_t, 4> {
        // [0]=last_user, [1]=last_tool, [2]=last_assistant_toolcall, [3]=last_assistant_final
        std::array<size_t, 4> idx = {std::string::npos, std::string::npos,
                                     std::string::npos, std::string::npos};
        for (size_t k = 0; k < selected.size(); k++) {
            auto r = get_json_value(selected[k], "role", BLANK_STRING);
            if (r == "user")  idx[0] = k;
            else if (r == "tool") idx[1] = k;
            else if (r == "assistant" && IsToolRelatedMessage(selected[k], r))  idx[2] = k;
            else if (r == "assistant" && !IsToolRelatedMessage(selected[k], r)) idx[3] = k;
        }
        return idx;
    };

    bool skip_overflow_loop = false;
    if (selected.size() > max_messages) {
        auto pre_idx = compute_last_indices();
        size_t system_count = 0;
        size_t protected_non_system = 0;
        for (size_t k = 0; k < selected.size(); k++) {
            auto r = get_json_value(selected[k], "role", BLANK_STRING);
            if (r == "system") { system_count++; continue; }
            if (r == "user"      && k == pre_idx[0]) { protected_non_system++; continue; }
            if (r == "tool"      && k == pre_idx[1]) { protected_non_system++; continue; }
            if (r == "assistant" &&  IsToolRelatedMessage(selected[k], r) && k == pre_idx[2]) { protected_non_system++; continue; }
            if (r == "assistant" && !IsToolRelatedMessage(selected[k], r) && k == pre_idx[3]) { protected_non_system++; continue; }
        }
        size_t non_system_count = selected.size() - system_count;
        if (protected_non_system >= non_system_count) {
            skip_overflow_loop = true;
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kWarning} << "[SmartSelect] Step3 overflow: All "
                    << non_system_count << " non-system messages are protected, "
                    << "skipping overflow loop (selected=" << selected.size()
                    << " > max_messages=" << max_messages << ")" << std::endl;
            }
        }
    }

    while (!skip_overflow_loop && selected.size() > max_messages) {
        // 每轮删除前重新计算最后索引（O(n)）
        auto last_idx = compute_last_indices();
        size_t last_user            = last_idx[0];
        size_t last_tool            = last_idx[1];
        size_t last_asst_toolcall   = last_idx[2];
        size_t last_asst_final      = last_idx[3];

        // 找到最旧的可删除消息（从前往后，跳过 system 和保护消息）
        bool removed = false;
        for (size_t i = 0; i < selected.size(); i++) {
            auto role = get_json_value(selected[i], "role", BLANK_STRING);
            if (role == "system") continue;

            if (role == "user"      && i == last_user)          continue;
            if (role == "tool"      && i == last_tool)          continue;
            if (role == "assistant" && IsToolRelatedMessage(selected[i], role)  && i == last_asst_toolcall) continue;
            if (role == "assistant" && !IsToolRelatedMessage(selected[i], role) && i == last_asst_final)    continue;

            // 找到可删除的消息
            if (role == "assistant" && IsToolRelatedMessage(selected[i], role)) {
                // 级联删除：删除 assistant tool_call 及其后续连续 tool 消息
                size_t erase_end = i + 1;
                while (erase_end < selected.size() &&
                       get_json_value(selected[erase_end], "role", BLANK_STRING) == "tool") {
                    erase_end++;
                }
                // 检查要删除的链中是否包含受保护的最后一条 tool 消息
                bool chain_has_protected_tool = (last_tool != std::string::npos &&
                                                 last_tool > i && last_tool < erase_end);
                if (chain_has_protected_tool) {
                    if (model_config_.getenablePromptDebug()) {
                        My_Log{My_Log::Level::kWarning} << "[SmartSelect] Step3 overflow: Skipping assistant+tool chain at ["
                                                         << i << "] because chain contains protected last tool message" << std::endl;
                    }
                    continue;
                }
                size_t drop_count = erase_end - i;
                // nlohmann::json v3.10.0 的 ordered_map 未提供 erase(iterator, iterator) 重载，
                // 因此逐个删除同一位置的元素来等效实现区间删除，而不是直接对 json 数组做范围 erase。
                for (size_t d = 0; d < drop_count; d++) {
                    selected.erase(selected.begin() + i);
                }
                selected_orig_indices.erase(selected_orig_indices.begin() + i,
                                            selected_orig_indices.begin() + erase_end);
                if (model_config_.getenablePromptDebug()) {
                    My_Log{My_Log::Level::kWarning} << "[SmartSelect] Step3 overflow: Removed assistant+tool chain at ["
                                                     << i << "] (" << drop_count << " msgs) to maintain max_messages="
                                                     << max_messages << std::endl;
                }
            } else {
                // 对于 tool 消息，检查其前面紧邻的 assistant tool_call 是否受保护
                // 若受保护，则不能单独删除该 tool 消息，否则 assistant 会成为孤立的工具调用请求
                if (role == "tool") {
                    bool prev_assistant_protected = false;
                    for (int j = (int)i - 1; j >= 0; j--) {
                        auto r = get_json_value(selected[j], "role", BLANK_STRING);
                        if (r == "assistant" && IsToolRelatedMessage(selected[j], r)) {
                            // 找到对应的 assistant，检查是否是受保护的最后一个 assistant tool_call
                            if (last_asst_toolcall != std::string::npos && (size_t)j == last_asst_toolcall) {
                                prev_assistant_protected = true;
                            }
                            break;
                        }
                        // 遇到非 tool/assistant 消息则停止向前查找
                        if (r != "tool") break;
                    }
                    if (prev_assistant_protected) {
                        if (model_config_.getenablePromptDebug()) {
                            My_Log{My_Log::Level::kWarning} << "[SmartSelect] Step3 overflow: Skipping tool msg["
                                                             << i << "] because its preceding assistant is protected"
                                                             << std::endl;
                        }
                        continue;
                    }
                }
                selected.erase(selected.begin() + i);
                selected_orig_indices.erase(selected_orig_indices.begin() + i);
                if (model_config_.getenablePromptDebug()) {
                    My_Log{My_Log::Level::kWarning} << "[SmartSelect] Step3 overflow: Removed msg[" << i
                                                     << "] role=" << role << " to maintain max_messages="
                                                     << max_messages << std::endl;
                }
            }
            removed = true;
            break;
        }
        // 如果没有可删除的消息（全部受保护），退出循环
        if (!removed) {
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kWarning} << "[SmartSelect] Step3 overflow: All messages protected, "
                                                 << "allowing " << selected.size() << " > max_messages="
                                                 << max_messages << std::endl;
            }
            break;
        }
    }

    if (model_config_.getenablePromptDebug()) {
        My_Log{My_Log::Level::kDebug} << "[SmartSelect] Output: " << selected.size() << " messages" << std::endl;
    }
    return selected;
}

// ============================================================
// ValidateToolCallSequence
// ============================================================

// 验证工具调用序列，确保每个 tool 消息前都有对应的 assistant 消息
json MessagePreFilter::ValidateToolCallSequence(const json& msg)
{
    json validated = json::array();

    for (size_t i = 0; i < msg.size(); i++) {
        const auto& element = msg[i];
        auto role = get_json_value(element, "role", BLANK_STRING);

        if (role == "tool") {
            // 检查前面是否有工具调用类型的 assistant 消息
            bool has_assistant = false;
            for (int j = validated.size() - 1; j >= 0; j--) {
                auto r = get_json_value(validated[j], "role", BLANK_STRING);
                if (r == "assistant" && IsToolRelatedMessage(validated[j], r)) {
                    has_assistant = true;
                    break;
                }
            }

            if (!has_assistant) {
                // 孤立的 tool 消息，记录警告并跳过
                My_Log{My_Log::Level::kWarning}
                    << "[ValidateToolCall] Skipping orphaned tool message at index " << i << std::endl;
                continue;
            }
        }

        // 检查 assistant tool_call 消息后是否紧跟至少一条 tool 响应
        // 若 assistant tool_call 后面没有 tool 响应，则该 assistant 消息也应被跳过
        if (role == "assistant" && IsToolRelatedMessage(element, role)) {
            bool has_tool_response = (i + 1 < msg.size() &&
                                      get_json_value(msg[i + 1], "role", BLANK_STRING) == "tool");
            if (!has_tool_response) {
                My_Log{My_Log::Level::kWarning}
                    << "[ValidateToolCall] Skipping orphaned assistant tool_call message at index " << i
                    << " (no following tool response)" << std::endl;
                continue;
            }
        }

        validated.push_back(element);
    }

    if (validated.size() < msg.size()) {
        My_Log{My_Log::Level::kWarning}
            << "[ValidateToolCall] Removed " << (msg.size() - validated.size())
            << " orphaned messages (tool without assistant, or assistant tool_call without tool response)"
            << std::endl;
    }

    return validated;
}

// ============================================================
// SafeEraseToolChain
// ============================================================

// 级联删除工具调用链：给定一个 assistant tool_call 消息的索引，删除它及其后所有连续的 tool responses
// 返回实际删除的消息数量
size_t MessagePreFilter::SafeEraseToolChain(json& messages, size_t assistant_idx)
{
    // 统计紧跟在 assistant 后面的连续 tool 消息数量
    size_t tool_count = 0;
    for (size_t k = assistant_idx + 1; k < messages.size(); k++) {
        auto role = get_json_value(messages[k], "role", BLANK_STRING);
        if (role == "tool") {
            tool_count++;
        } else {
            break;
        }
    }
    size_t drop_count = 1 + tool_count;
    // nlohmann::json v3.10.0 的 ordered_map 未提供 erase(iterator, iterator) 重载，
    // 因此逐个删除同一位置的元素来等效实现区间删除。
    for (size_t d = 0; d < drop_count; d++) {
        messages.erase(messages.begin() + assistant_idx);
    }
    return drop_count;
}

// ============================================================
// DropOldMessagesBatch
// ============================================================

// 批量删除旧消息直到满足预算（Phase 1 专用）
// 贪心 + 批量删除策略（O(n)）
void MessagePreFilter::DropOldMessagesBatch(
    json& filtered_msg,
    const std::vector<bool>& old_flags,
    const std::function<bool(const json&, size_t)>& is_protected,
    std::shared_ptr<ContextBase> handle,
    size_t& current_tokens,
    size_t available_tokens,
    const std::function<void()>& refresh_prot_idx)
{
    if (current_tokens <= available_tokens) return;  // 已满足预算，无需删除

    // 更新保护索引缓存（批量删除前只需调用一次）
    if (refresh_prot_idx) refresh_prot_idx();

    // 步骤 1：预计算每条消息的 token 数，并确定删除集合
    size_t n = filtered_msg.size();
    std::vector<bool> to_delete(n, false);
    size_t tokens_to_free = current_tokens - available_tokens;  // 需要释放的 token 数
    size_t freed_tokens = 0;

    size_t non_system_total = 0;
    size_t non_system_tokens = 0;
    std::vector<size_t> msg_tokens_cache(n, 0);
    for (size_t k = 0; k < n; ++k) {
        std::string counted = get_json_value(filtered_msg[k], "content", BLANK_STRING);
        const std::string k_role = get_json_value(filtered_msg[k], "role", BLANK_STRING);
        if (k_role == "assistant" && filtered_msg[k].contains("tool_calls") &&
            filtered_msg[k]["tool_calls"].is_array() && !filtered_msg[k]["tool_calls"].empty()) {
            counted += filtered_msg[k]["tool_calls"].dump();
        }
        msg_tokens_cache[k] = handle->TokenLength(counted);
        if (k_role == "system") continue;
        non_system_total++;
        non_system_tokens += msg_tokens_cache[k];
    }
    const size_t avg_tokens = non_system_total > 0 ? std::max<size_t>(1, non_system_tokens / non_system_total) : 1;
    const size_t survivor_floor = std::min<size_t>(
        std::max<size_t>(kMinSurvivorFloor, available_tokens / (2 * avg_tokens)), 2 * kMinSurvivorFloor);
    size_t deleted_count = 0;
    bool floor_hit = false;
    size_t repeat_retained_tokens = 0;
    size_t repeat_total_retained_tokens = 0;

    size_t i = 0;
    while (i < n && freed_tokens < tokens_to_free) {
        auto role = get_json_value(filtered_msg[i], "role", BLANK_STRING);

        // 跳过 system 消息
        if (role == "system") { i++; continue; }

        // 跳过非旧消息（old_flags[i] == false）
        if (!old_flags[i]) { i++; continue; }

        // 跳过受保护的消息
        if (is_protected(filtered_msg, i)) { i++; continue; }

        if (role == "assistant" && IsToolRelatedMessage(filtered_msg[i], role)) {
            // assistant tool_call：级联删除其后的连续 tool responses
            size_t chain_end = i + 1;
            while (chain_end < n &&
                   get_json_value(filtered_msg[chain_end], "role", BLANK_STRING) == "tool") {
                chain_end++;
            }

            if (PrefixCacheActive() && (!repeat_protected_raw_.empty() || !repeat_must_keep_raw_.empty())) {
                size_t repeat_raw = SIZE_MAX;
                bool chain_must_keep = false;
                for (size_t k = i; k < chain_end; ++k) {
                    if (!filtered_msg[k].is_object() || !filtered_msg[k].contains(kRawIdxKey)) continue;
                    const size_t raw_idx = filtered_msg[k][kRawIdxKey].get<size_t>();
                    if (repeat_must_keep_raw_.count(raw_idx)) { repeat_raw = raw_idx; chain_must_keep = true; break; }
                    if (repeat_raw == SIZE_MAX && repeat_protected_raw_.count(raw_idx)) repeat_raw = raw_idx;
                }
                if (repeat_raw != SIZE_MAX) {
                    size_t chain_tokens_probe = 0;
                    for (size_t k = i; k < chain_end; ++k) chain_tokens_probe += msg_tokens_cache[k];
                    if (chain_must_keep) {
                        repeat_total_retained_tokens += chain_tokens_probe;
                        My_Log{My_Log::Level::kInfo} << "[RepeatRead] action=keep idx=" << repeat_raw
                            << " tok=" << chain_tokens_probe << " must_keep=1" << std::endl;
                        i = chain_end;
                        continue;
                    }
                    const size_t retain_cap_normal = static_cast<size_t>(
                        static_cast<double>(repeat_retain_budget_) * kRepeatRetainRatio);
                    const size_t retain_cap_total = static_cast<size_t>(
                        static_cast<double>(repeat_retain_budget_) * kRepeatRetainExceptionRatio);
                    const bool fits_normal =
                        (repeat_retained_tokens + chain_tokens_probe <= retain_cap_normal) &&
                        (repeat_total_retained_tokens + chain_tokens_probe <= retain_cap_total);
                    if (fits_normal) {
                        repeat_retained_tokens += chain_tokens_probe;
                        repeat_total_retained_tokens += chain_tokens_probe;
                        My_Log{My_Log::Level::kInfo} << "[RepeatRead] action=keep idx=" << repeat_raw
                            << " tok=" << chain_tokens_probe << std::endl;
                        i = chain_end;
                        continue;
                    }
                    My_Log{My_Log::Level::kInfo} << "[RepeatRead] action=cap_drop idx=" << repeat_raw
                        << " tok=" << chain_tokens_probe << std::endl;
                }
            }

            if (non_system_total - deleted_count - (chain_end - i) < survivor_floor) {
                floor_hit = true;
                break;
            }
            size_t chain_tokens = 0;
            for (size_t k = i; k < chain_end; k++) {
                chain_tokens += msg_tokens_cache[k];
                to_delete[k] = true;
            }
            deleted_count += chain_end - i;
            freed_tokens += chain_tokens;
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter]   Phase 1"
                    << ": Batch-drop assistant+tool chain at ["
                    << i << "] (" << (chain_end - i) << " msgs, "
                    << chain_tokens << " tok)" << std::endl;
            }
            prefilter_stats_.dropped_by_token_limit += (chain_end - i);
            prefilter_stats_.total_dropped += (chain_end - i);
            i = chain_end;  // 跳过整条链
        } else {
            if (non_system_total - deleted_count - 1 < survivor_floor) {
                floor_hit = true;
                break;
            }
            size_t msg_tokens = msg_tokens_cache[i];
            to_delete[i] = true;
            deleted_count++;
            freed_tokens += msg_tokens;
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter]   Phase 1"
                    << ": Batch-drop msg[" << i
                    << "] role=" << role
                    << " (" << msg_tokens << " tok)" << std::endl;
            }
            prefilter_stats_.dropped_by_token_limit++;
            prefilter_stats_.total_dropped++;
            i++;
        }
    }

    if (floor_hit && freed_tokens < tokens_to_free) {
        const size_t condense_min_tokens = std::max<size_t>(1, available_tokens / kSurvivorCondenseMinShare);
        size_t excess = tokens_to_free - freed_tokens;
        size_t largest = n;
        for (size_t k = 0; k < n; ++k) {
            if (to_delete[k]) continue;
            const std::string r = get_json_value(filtered_msg[k], "role", BLANK_STRING);
            if (r == "system" || r == "user") continue;
            if (msg_tokens_cache[k] < condense_min_tokens) continue;
            if (PrefixCacheActive() && filtered_msg[k].is_object() && filtered_msg[k].contains(kRawIdxKey) &&
                repeat_must_keep_raw_.count(filtered_msg[k][kRawIdxKey].get<size_t>())) continue;
            if (largest == n || msg_tokens_cache[k] > msg_tokens_cache[largest]) largest = k;
        }
        if (largest != n) {
            const std::string content = get_json_value(filtered_msg[largest], "content", BLANK_STRING);
            const size_t old_tok = msg_tokens_cache[largest];
            const size_t target_tok = old_tok > excess + condense_min_tokens / 2
                ? old_tok - excess : condense_min_tokens / 2;
            CondenseBudget budget;
            budget.max_tokens = target_tok;
            budget.max_chars = content.size() * target_tok / std::max<size_t>(1, old_tok);
            std::function<size_t(const std::string&)> tl = [&](const std::string& s) { return handle->TokenLength(s); };
            const std::string condensed = ContentCondenser::Condense(content, ContentKind::kToolResponse, budget, &tl).text;
            const size_t new_tok = handle->TokenLength(condensed);
            if (new_tok < old_tok) {
                filtered_msg[largest]["content"] = condensed;
                freed_tokens += old_tok - new_tok;
                prefilter_stats_.truncated_count++;
            }
            My_Log{My_Log::Level::kWarning} << "[PreFilter] Survivor floor hit: floor=" << survivor_floor
                                             << " non_system=" << non_system_total - deleted_count
                                             << " avg_tok=" << avg_tokens << "; condensed msg[" << largest
                                             << "] " << old_tok << "->" << new_tok << " tok instead of dropping" << std::endl;
        } else {
            My_Log{My_Log::Level::kWarning} << "[PreFilter] Survivor floor hit: floor=" << survivor_floor
                                             << " non_system=" << non_system_total - deleted_count
                                             << "; no condensable survivor" << std::endl;
        }
    }

    for (size_t k = n; k-- > 0; ) {
        if (to_delete[k]) {
            filtered_msg.erase(filtered_msg.begin() + k);
        }
    }

    // 步骤 3：更新 current_tokens（饱和减法防止下溢）
    if (current_tokens >= freed_tokens) {
        current_tokens -= freed_tokens;
    } else {
        current_tokens = 0;
    }
}

// ============================================================
// CompressMessages
// ============================================================

// 通用辅助函数：压缩指定角色的消息内容
// 统一处理 Phase 2（旧 user/assistant）、Phase 3（新 user/assistant）、Phase 4（tool）的压缩逻辑
size_t MessagePreFilter::CompressMessages(
    json& filtered_msg,
    const std::vector<bool>& old_flags,
    const std::function<bool(const json&)>& within_budget,
    const std::function<bool(const json&, size_t)>& is_truncate_protected,
    const std::function<std::string(const std::string&, size_t)>& truncate_content,
    const std::string& target_role,
    bool only_old,
    size_t max_len,
    size_t tool_min_length,
    const std::string& phase_name,
    std::shared_ptr<ContextBase> handle,
    size_t* current_tokens,
    size_t available_tokens,
    bool is_harmony)
{
    bool is_tool_phase = (target_role == "tool");
    size_t compressed_count = 0;

    // 当 available_tokens == 0 时，system prompt 已占满整个 context，
    // 消息内容压缩无法解决根本问题，由 FitMessagesToContext 作为最终兜底处理
    if (available_tokens == 0) {
        return 0;
    }

    bool use_incremental = (handle != nullptr && current_tokens != nullptr);

    for (size_t i = 0; i < filtered_msg.size(); i++) {
        auto role = get_json_value(filtered_msg[i], "role", BLANK_STRING);

        // ── 角色过滤 ──────────────────────────────────────────────────────
        if (is_tool_phase) {
            if (role != "tool") continue;
            if (only_old && !old_flags[i]) continue;
            if (!only_old && old_flags[i]) continue;
        } else {
            // user_assistant 模式：跳过 system 和 tool
            if (role == "system" || role == "tool") continue;
            if (only_old && !old_flags[i]) continue;
            if (!only_old && old_flags[i]) continue;
        }

        // ── 截断保护检查 ──────────────────────────────────────────────────
        if (is_truncate_protected(filtered_msg, i)) {
            if (is_tool_phase && model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter]   " << phase_name
                    << ": tool msg[" << i << "] is truncate-protected (last tool msg), skipping" << std::endl;
            }
            continue;
        }

        // ── 执行截断 ──────────────────────────────────────────────────────
        bool is_old = old_flags[i];
        std::string content = get_json_value(filtered_msg[i], "content", BLANK_STRING);
        std::string truncated = truncate_content(content, max_len);
        if (truncated.length() < content.length()) {
            // tool 消息：新消息压缩后不得低于 tool_min_length
            if (is_tool_phase && !is_old && tool_min_length > 0 &&
                truncated.length() < tool_min_length) {
                if (model_config_.getenablePromptDebug()) {
                    My_Log{My_Log::Level::kWarning} << "[PreFilter]   " << phase_name
                        << ": tool msg[" << i << "] would be " << truncated.length()
                        << " chars (< min " << tool_min_length << "), skipping" << std::endl;
                }
                continue;
            }

            filtered_msg[i]["content"] = truncated;
            compressed_count++;

            if (model_config_.getenablePromptDebug()) {
                if (is_tool_phase) {
                    My_Log{My_Log::Level::kInfo} << "[PreFilter]   " << phase_name
                        << ": tool msg[" << i << "] (" << (is_old ? "old" : "recent") << ") "
                        << content.length() << "->" << truncated.length() << " chars" << std::endl;
                } else {
                    My_Log{My_Log::Level::kInfo} << "[PreFilter]   " << phase_name
                        << ": msg[" << i << "] role=" << role << " "
                        << content.length() << "->" << truncated.length() << " chars" << std::endl;
                }
            }

            // 满足预算后提前退出，避免不必要的压缩（有意为之的保守策略）
            if (use_incremental) {
                // 增量 token 计数时，tool 消息需要加入与 calc_msg_tokens 相同的包装开销
                std::string old_for_count = content;
                std::string new_for_count = truncated;
                if (is_tool_phase) {
                    if (!is_harmony) {
                        old_for_count = "<tool_response>\n" + content + "\n</tool_response>\n";
                        new_for_count = "<tool_response>\n" + truncated + "\n</tool_response>\n";
                    } else {
                        old_for_count = HarmonyProcessor::BuildToolMessage("unknown_tool", content);
                        new_for_count = HarmonyProcessor::BuildToolMessage("unknown_tool", truncated);
                    }
                } else if (is_harmony && role == "user") {
                    old_for_count = HarmonyProcessor::BuildUserMessage(content);
                    new_for_count = HarmonyProcessor::BuildUserMessage(truncated);
                }
                size_t old_tokens = handle->TokenLength(old_for_count);
                size_t new_tokens = handle->TokenLength(new_for_count);
                if (*current_tokens >= old_tokens) {
                    *current_tokens = *current_tokens - old_tokens + new_tokens;
                } else {
                    My_Log{My_Log::Level::kWarning}
                        << "[CompressMessages] current_tokens underflow guard triggered at msg["
                        << i << "] old=" << old_tokens << " cur=" << *current_tokens << std::endl;
                    *current_tokens = (new_tokens >= old_tokens)
                                      ? (*current_tokens + (new_tokens - old_tokens))
                                      : 0;
                }
                if (*current_tokens <= available_tokens) break;
            } else {
                // 回退：全量 token 计算（O(n) per compression，整体 O(n²)）
                if (within_budget(filtered_msg)) break;
            }
        }
    }
    return compressed_count;
}

// ============================================================
// PreFilterMessages
// ============================================================

// 消息预过滤函数（分级压缩算法）
// 所有阈值均从 service_config.json 的 prompt_optimization 节加载
nlohmann::ordered_json MessagePreFilter::PreFilterMessages(
    json& msg,
    int contextSize,
    const std::string& system_prompt_for_token_calc,
    bool is_harmony,
    const std::unordered_map<std::string, std::string>* tool_call_id_to_name)
{
    const auto& cfg = model_config_.GetPromptOptimizationConfig();

    if (model_config_.getenablePromptDebug()) {
        My_Log{My_Log::Level::kInfo}.original(true) << std::endl;
        My_Log{My_Log::Level::kInfo} << "\n========== PreFilterMessages (Tiered Compression) ==========" << std::endl;
        My_Log{My_Log::Level::kInfo} << "[PreFilter] Input: " << msg.size() << " messages" << std::endl;
    }

    // 重置统计信息
    prefilter_stats_.Reset();
    prefilter_dropped_messages_.clear();

    // 获取 model handle（提前到 Step 1 之前：token 预算感知裁剪判断需要用它计算 token 使用率）
    // 修复：多模型场景下优先使用 context_override_（per-model 的 ContextBase），
    // 而非 model_config_.get_genie_model_handle()（全局单模型句柄）
    std::shared_ptr<ContextBase> handle;
    if (context_override_) {
        // 使用非拥有 shared_ptr 包装 context_override_（生命周期由调用方保证）
        handle = std::shared_ptr<ContextBase>(context_override_, [](ContextBase*){});
    } else {
        handle = model_config_.get_genie_model_handle().lock();
    }

    // 计算 system tokens 和 available_tokens（同样提前到 Step 1 之前）
    size_t system_tokens = 0;
    size_t available_tokens = 0;
    if (handle) {
        if (!system_prompt_for_token_calc.empty()) {
            system_tokens = handle->TokenLength(system_prompt_for_token_calc);
        }
        available_tokens = (static_cast<size_t>(contextSize) > system_tokens)
                            ? (static_cast<size_t>(contextSize) - system_tokens)
                            : 0;
    }

    // 统一 token 计算口径：只计算 content 字段的 tokens（提前到 Step 1 之前定义，使 Step 1
    // 的预算判断与后续 Phase 0-5 共用同一套包装开销逻辑，避免两处口径不一致）
    // tool 消息需要加入格式包装开销（普通路径：OptimizeToolResponse 包装；Harmony 路径：BuildToolMessage 包装）
    auto calc_msg_tokens = [&](const json& messages) -> size_t {
        size_t total = 0;
        for (const auto& element : messages) {
            auto role = get_json_value(element, "role", BLANK_STRING);
            if (role == "system") continue;
            std::string content = get_json_value(element, "content", BLANK_STRING);
            if (!is_harmony && role == "tool") {
                // 普通路径：加入 OptimizeToolResponse 包装开销
                content = "<tool_response>\n" + content + "\n</tool_response>\n";
            } else if (is_harmony && role == "tool") {
                // Harmony 路径：使用 HarmonyProcessor::BuildToolMessage() 生成估算内容
                std::string actual_tool_name = "unknown_tool";
                if (element.contains("name") && element["name"].is_string()) {
                    actual_tool_name = element["name"].get<std::string>();
                } else if (element.contains("tool_call_id") && element["tool_call_id"].is_string()) {
                    std::string call_id = element["tool_call_id"].get<std::string>();
                    if (tool_call_id_to_name) {
                        auto it = tool_call_id_to_name->find(call_id);
                        if (it != tool_call_id_to_name->end()) {
                            actual_tool_name = it->second;
                        }
                    }
                }
                content = HarmonyProcessor::BuildToolMessage(actual_tool_name, content);
            } else if (is_harmony && role == "user") {
                // Harmony 路径：user 消息在传入 FitMessagesToContext 之前会被包装为 Harmony 格式
                content = HarmonyProcessor::BuildUserMessage(content);
            } else if (role == "assistant" && element.contains("tool_calls") &&
                       element["tool_calls"].is_array() && !element["tool_calls"].empty()) {
                content += element["tool_calls"].dump();
            }
            total += handle->TokenLength(content);
        }
        return total;
    };

    // ── Step 1: 消息数量限制（cfg.max_messages_limit）── token 预算感知 ──────
    // 仅当"条数超限"且"token 使用率达到压力阈值 cfg.token_pressure_trigger_ratio"时才真正
    // 触发 SmartSelectMessages 裁剪；否则哪怕条数超限，只要预算充足就不裁剪，交由后续
    // Phase 0-5/FitMessagesToContext 的 token-aware 逻辑处理。无 model handle 时无法计算
    // token 使用率，保持旧行为（纯按条数裁剪，视为预算始终紧张）。
    const bool prefix_cache_active = PrefixCacheActive();
    const std::string evict_key = (handle && prefix_cache_active) ? TaskMemoBuilder::ComputeFirstMsgKey(msg) : std::string();
    EvictionState prior_state;
    std::string invalid_reason;
    const bool have_prior = !evict_key.empty() &&
        LookupEvictionState(evict_key, handle.get(), msg, prior_state, invalid_reason);
    const std::unordered_set<size_t> prior_dropped_set(prior_state.dropped.begin(), prior_state.dropped.end());
    const bool eviction_state_unknown = !evict_key.empty() && !have_prior && invalid_reason != "first_eviction";
    const json msg_raw_for_hash = evict_key.empty() ? json() : msg;
    const size_t tool_entry_cap_tokens = (contextSize > 0)
        ? static_cast<size_t>(static_cast<float>(contextSize) * kToolEntryCapRatio) : 0;
    const size_t must_keep_budget_total = static_cast<size_t>(
        static_cast<double>(available_tokens) * kRepeatRetainExceptionRatio);
    bool smart_select_possible = false;
    double smart_select_pressure = 0.0;
    size_t smart_select_msgs = 0;
    if (!evict_key.empty()) {
        json pre_select = json::array();
        for (size_t i = 0; i < msg.size(); ++i) {
            if (!prior_dropped_set.count(i)) pre_select.push_back(msg[i]);
        }
        smart_select_msgs = pre_select.size();
        if (pre_select.size() > cfg.max_messages_limit) {
            smart_select_possible = true;
            if (available_tokens > 0) {
                smart_select_pressure = static_cast<double>(calc_msg_tokens(pre_select)) /
                    static_cast<double>(available_tokens);
                smart_select_possible = smart_select_pressure >= cfg.token_pressure_trigger_ratio;
            }
        }
    }
    const size_t must_keep_budget = smart_select_possible ? 0 : must_keep_budget_total;
    if (!evict_key.empty()) {
        My_Log{My_Log::Level::kInfo} << "[PreFilter] Smart select: possible=" << (smart_select_possible ? 1 : 0)
                                      << " msgs=" << smart_select_msgs << "/" << cfg.max_messages_limit
                                      << " pressure=" << smart_select_pressure
                                      << " must_keep_budget=" << must_keep_budget << "/" << must_keep_budget_total
                                      << std::endl;
    }
    ApplyRepeatResultStubs(msg, evict_key, handle.get(), prior_dropped_set, eviction_state_unknown,
                           tool_entry_cap_tokens, must_keep_budget,
                           [this](const std::string& t) { return CountTokens(t); },
                           repeat_protected_raw_, repeat_must_keep_raw_);
    repeat_retain_budget_ = available_tokens;
    json source = msg;
    for (size_t i = 0; i < source.size(); ++i) {
        if (source[i].is_object()) source[i][kRawIdxKey] = i;
    }
    const json raw_tagged = source;
    if (have_prior) {
        json kept_source = json::array();
        for (const auto& m : source) {
            if (m.is_object() && m.contains(kRawIdxKey) &&
                prior_dropped_set.count(m[kRawIdxKey].get<size_t>())) continue;
            kept_source.push_back(m);
        }
        source = std::move(kept_source);
        prefilter_stats_.dropped_by_token_limit += prior_state.dropped.size();
        prefilter_stats_.total_dropped += prior_state.dropped.size();
    } else if (!evict_key.empty() && invalid_reason != "first_eviction") {
        My_Log{My_Log::Level::kInfo} << "[PreFilter] Eviction state invalidated: reason=" << invalid_reason << std::endl;
    }

    const size_t max_messages = cfg.max_messages_limit;
    json filtered_msg = json::array();
    const bool count_exceeded = (source.size() > max_messages);
    bool budget_pressure = true;
    size_t current_msg_tokens = 0;
    if (count_exceeded && handle && available_tokens > 0) {
        current_msg_tokens = calc_msg_tokens(source);
        double usage_ratio = static_cast<double>(current_msg_tokens) / static_cast<double>(available_tokens);
        budget_pressure = (usage_ratio >= cfg.token_pressure_trigger_ratio);
    }

    if (count_exceeded && budget_pressure) {
        filtered_msg = SmartSelectMessages(source, max_messages);
        size_t dropped = source.size() - filtered_msg.size();
        prefilter_stats_.dropped_by_smart_select = dropped;
        prefilter_stats_.total_dropped += dropped;
        if (model_config_.getenablePromptDebug()) {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Step 1: Smart limited to " << filtered_msg.size()
                                           << " messages (dropped " << dropped << ", token_usage="
                                           << current_msg_tokens << "/" << available_tokens << ")" << std::endl;
        }
    } else {
        filtered_msg = source;
        if (model_config_.getenablePromptDebug()) {
            if (count_exceeded) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Step 1: Count exceeded (" << source.size()
                                               << " > " << max_messages << ") but token budget sufficient ("
                                               << current_msg_tokens << "/" << available_tokens
                                               << "), skip trimming" << std::endl;
            } else {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Step 1: All messages kept (total: " << msg.size() << ")" << std::endl;
            }
        }
    }

    if (!handle) {
        if (model_config_.getenablePromptDebug()) {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Skipped (no model handle)" << std::endl;
        }
        CaptureDroppedMessages(msg, filtered_msg);
        StripRawIdx(filtered_msg);
        return filtered_msg;
    }

    // ── [DIAG] 诊断打印辅助 Lambda ────────────────────────────────────────
    // 打印最终会发送给模型的所有内容（system prompt + 压缩后的 messages）
    // 包括：user/assistant/tool 消息的完整内容、assistant 的 tool_calls、tool 的返回结果
    // 每条消息附带 token 数和字符数，便于分析哪部分占用 tokens 较多
    auto print_diag_dump = [&](const json& final_msgs) {
        // 1. 打印 system prompt（一次性完整打印）
        My_Log{My_Log::Level::kWarning}
            << "[PreFilter] [DIAG] ========== System Prompt ("
            << system_tokens << " tok, " << system_prompt_for_token_calc.size() << " chars) ==========\n"
            << system_prompt_for_token_calc << std::endl;

        // 2. 逐条打印所有 messages（含 content + tool_calls）
        My_Log{My_Log::Level::kWarning}
            << "[PreFilter] [DIAG] ========== Messages (" << final_msgs.size() << " total) ==========" << std::endl;

        for (size_t diag_i = 0; diag_i < final_msgs.size(); ++diag_i) {
            const auto& diag_msg = final_msgs[diag_i];
            std::string diag_role = get_json_value(diag_msg, "role", BLANK_STRING);

            // 跳过 system 消息：system prompt 已在上方单独打印（优化后版本），
            // 此处打印的是原始客户端发送的 system 消息，内容不同且冗余
            if (diag_role == "system") continue;

            // 提取 content（可能为空，如 assistant tool_call 消息）
            std::string diag_content;
            if (diag_msg.contains("content") && !diag_msg["content"].is_null()) {
                if (diag_msg["content"].is_string()) {
                    diag_content = diag_msg["content"].get<std::string>();
                } else {
                    diag_content = diag_msg["content"].dump();
                }
            }

            // 提取 tool_calls（assistant 发起工具调用时）
            std::string diag_tool_calls;
            if (diag_msg.contains("tool_calls") && !diag_msg["tool_calls"].is_null() &&
                diag_msg["tool_calls"].is_array() && !diag_msg["tool_calls"].empty()) {
                diag_tool_calls = diag_msg["tool_calls"].dump(2);
            }

            // 计算 token 数（content + tool_calls 合并计算）
            std::string diag_full = diag_content;
            if (!diag_tool_calls.empty()) {
                if (!diag_full.empty()) diag_full += "\n";
                diag_full += diag_tool_calls;
            }
            size_t diag_tok = handle->TokenLength(diag_full);

            // 打印消息头（role、token 数、字符数）
            std::string diag_header = "[PreFilter] [DIAG] --- msg[" + std::to_string(diag_i)
                + "] role=" + diag_role
                + " (" + std::to_string(diag_tok) + " tok, " + std::to_string(diag_full.size()) + " chars)";
            if (!diag_tool_calls.empty()) diag_header += " [has tool_calls]";
            diag_header += " ---";
            My_Log{My_Log::Level::kWarning} << diag_header << "\n";

            // 打印 content（非空时）
            if (!diag_content.empty()) {
                My_Log{My_Log::Level::kWarning} << diag_content << std::endl;
            }
            // 打印 tool_calls（非空时）
            if (!diag_tool_calls.empty()) {
                My_Log{My_Log::Level::kWarning} << "[tool_calls]:\n" << diag_tool_calls << std::endl;
            }
        }

        My_Log{My_Log::Level::kWarning}
            << "[PreFilter] [DIAG] ========== End of Diagnostic Dump ==========" << std::endl;
    };

    // 当 system prompt 已占满或超出 context 时，直接跳过所有 Phase，由 FitMessagesToContext 处理
    if (available_tokens == 0) {
        My_Log{My_Log::Level::kWarning}
            << "[PreFilter] System prompt (" << system_tokens
            << " tok) exceeds context size (" << contextSize
            << " tok), skipping all phases, FitMessagesToContext will handle" << std::endl;
        // 打印诊断信息（system prompt 超出时，messages 尚未压缩，但仍有参考价值）
        print_diag_dump(filtered_msg);
        CaptureDroppedMessages(msg, filtered_msg);
        StripRawIdx(filtered_msg);
        return filtered_msg;
    }

    // ── 辅助 Lambda ──────────────────────────────────────────────────────
    // calc_msg_tokens 已提前到 Step 1 之前定义（供 token 预算感知裁剪复用），此处不再重复定义。

    auto within_budget = [&](const json& messages) -> bool {
        return calc_msg_tokens(messages) <= available_tokens;
    };

    // 预计算各类消息的最后索引（O(n)），供 is_protected / is_truncate_protected 使用
    // 返回值：{last_user, last_asst_final, last_asst_toolcall, last_tool}
    auto compute_protection_indices = [&](const json& messages)
        -> std::array<size_t, 4>
    {
        // [0]=last_user, [1]=last_asst_final, [2]=last_asst_toolcall, [3]=last_tool
        std::array<size_t, 4> idx = {
            std::string::npos, std::string::npos,
            std::string::npos, std::string::npos
        };
        for (size_t k = 0; k < messages.size(); k++) {
            auto r = get_json_value(messages[k], "role", BLANK_STRING);
            if (r == "user")
                idx[0] = k;
            else if (r == "tool")
                idx[3] = k;
            else if (r == "assistant" && !IsToolRelatedMessage(messages[k], r))
                idx[1] = k;
            else if (r == "assistant" &&  IsToolRelatedMessage(messages[k], r))
                idx[2] = k;
        }
        return idx;
    };

    // 判断消息是否受"丢弃保护"（不可被 Phase 1/5 删除）
    std::array<size_t, 4> current_prot_idx = compute_protection_indices(filtered_msg);

    auto is_protected = [&](const json& messages, size_t idx) -> bool {
        const auto& element = messages[idx];
        auto role = get_json_value(element, "role", BLANK_STRING);
        if (role == "user")      return idx == current_prot_idx[0];
        if (role == "tool")      return idx == current_prot_idx[3];
        if (role == "assistant" && !IsToolRelatedMessage(element, role))
                                 return idx == current_prot_idx[1];
        if (role == "assistant" &&  IsToolRelatedMessage(element, role))
                                 return idx == current_prot_idx[2];
        return false;
    };

    // 判断消息是否受"截断保护"（不可被 Phase 2/3/4 截断内容）
    auto is_truncate_protected = [&](const json& messages, size_t idx) -> bool {
        const auto& element = messages[idx];
        auto role = get_json_value(element, "role", BLANK_STRING);
        if (role == "user")      return idx == current_prot_idx[0];
        if (role == "tool")      return idx == current_prot_idx[3];
        if (role == "assistant" && !IsToolRelatedMessage(element, role))
                                 return idx == current_prot_idx[1];
        if (role == "assistant" &&  IsToolRelatedMessage(element, role))
                                 return idx == current_prot_idx[2];
        return false;
    };

    // 预计算每条消息是否是"旧消息"的标志数组
    auto precompute_old_flags = [&](const json& messages) -> std::vector<bool> {
        size_t non_system_count = 0;
        for (const auto& e : messages) {
            if (e["role"] != "system") non_system_count++;
        }
        size_t old_cutoff = (non_system_count > cfg.recent_window)
                            ? (non_system_count - cfg.recent_window)
                            : 0;
        std::vector<bool> flags(messages.size(), false);
        size_t non_system_idx = 0;
        for (size_t i = 0; i < messages.size(); i++) {
            if (messages[i]["role"] == "system") continue;
            if (non_system_idx < old_cutoff) flags[i] = true;
            non_system_idx++;
        }
        return flags;
    };

    auto mask_repeat_protected = [&](const json& messages, std::vector<bool>& flags, bool skip_value) {
        if (PrefixCacheActive() && (!repeat_protected_raw_.empty() || !repeat_must_keep_raw_.empty())) {
            auto is_repeat_protected_raw = [&](size_t raw_idx) {
                return repeat_protected_raw_.count(raw_idx) != 0 || repeat_must_keep_raw_.count(raw_idx) != 0;
            };
            for (size_t i = 0; i < messages.size();) {
                auto role = get_json_value(messages[i], "role", BLANK_STRING);
                if (role == "assistant" && IsToolRelatedMessage(messages[i], role)) {
                    size_t chain_end = i + 1;
                    while (chain_end < messages.size() &&
                           get_json_value(messages[chain_end], "role", BLANK_STRING) == "tool") {
                        chain_end++;
                    }
                    bool is_chain_protected = false;
                    for (size_t k = i; k < chain_end; ++k) {
                        if (messages[k].is_object() && messages[k].contains(kRawIdxKey) &&
                            is_repeat_protected_raw(messages[k][kRawIdxKey].get<size_t>())) {
                            is_chain_protected = true;
                            break;
                        }
                    }
                    if (is_chain_protected) {
                        for (size_t k = i; k < chain_end; ++k) flags[k] = skip_value;
                    }
                    i = chain_end;
                } else {
                    if (messages[i].is_object() && messages[i].contains(kRawIdxKey) &&
                        is_repeat_protected_raw(messages[i][kRawIdxKey].get<size_t>())) {
                        flags[i] = skip_value;
                    }
                    i++;
                }
            }
        }
    };

    // 保真截断（ContentCondenser）：高信号行提取 + 头尾双保留，取代旧的"只保留前缀/仅保头弃尾"
    // 策略——报错行、退出码、汇总结论多出现在内容尾部，不应被整段丢弃。
    //
    // Step 4：预算单位改为 token 口径（cfg.fidelity.budget_unit=="tokens"，默认）——现有字符
    // 阈值（max_len，即 old/recent/tool_compress_len）按 4 字符≈1 token 的保守估算换算为
    // token 预算；字符阈值本身继续原样传入 budget.max_chars，充当 Condense 内部第 1 步"长度
    // 快筛"的判据与最终硬上限，不产生新魔数。budget_unit=="chars" 时 token_len_ptr 为
    // nullptr、max_tokens=0，逐字节回退到 Step 3（纯字符模式，不接入 tokenizer）。
    //
    // 性能护栏：token_len_fn 绑定到 CountTokens()（与 FitMessagesToContext 同一套 per-model
    // handle 优先、无 handle 时粗略估算的逻辑），只有 Condense 内部快筛判定"内容明显超预算、
    // 需要收敛"时才会被调用，且二分探测次数不超过 cfg.fidelity.max_token_probe_per_message
    // （CondenseBudget::max_token_probe）——短消息（长度 ≤ 字符预算）在第 1 步即原样返回，
    // 完全不触发 tokenizer 调用。
    const auto& fidelity_cfg = cfg.fidelity;
    const bool use_token_budget = (fidelity_cfg.budget_unit == "tokens");
    std::function<size_t(const std::string&)> token_len_fn = [this](const std::string& text) {
        return CountTokens(text);
    };
    const std::function<size_t(const std::string&)>* token_len_ptr = use_token_budget ? &token_len_fn : nullptr;

    // 统一构造 CondenseBudget：preserve_tail=false 时 tail_ratio 强制为 0（等价于仅保留头部，
    // 逐字节回退到该特性引入之前的行为），extract_high_signal/json_head_items/json_tail_items/
    // max_token_probe 均直接来自 cfg.fidelity，关闭对应开关即整段回退。
    // P1：max_len -> max_tokens 的换算不再统一按 4:1（该比例只对纯 ASCII 文本成立），而是按
    // content 内实际 CJK 字节占比推算有效 bytes/token
    // （EstimateCjkAwareBytesPerToken，utils.h）；budget 本身也带上这两个比例，供
    // ContentCondenser::Condense 内部的硬截断兜底复用同一口径。
    auto make_condense_budget = [&](const std::string& content, size_t max_len) -> CondenseBudget {
        CondenseBudget budget;
        budget.max_chars = max_len;
        budget.cjk_bytes_per_token = fidelity_cfg.cjk_bytes_per_token;
        budget.ascii_bytes_per_token = fidelity_cfg.ascii_bytes_per_token;
        if (use_token_budget) {
            double bytes_per_token = EstimateCjkAwareBytesPerToken(
                content, fidelity_cfg.cjk_bytes_per_token, fidelity_cfg.ascii_bytes_per_token);
            budget.max_tokens = std::max<size_t>(1, static_cast<size_t>(
                static_cast<double>(max_len) / bytes_per_token));
        }
        budget.tail_ratio = fidelity_cfg.preserve_tail ? fidelity_cfg.tail_ratio : 0.0;
        budget.extract_high_signal = fidelity_cfg.extract_high_signal;
        budget.json_head_items = fidelity_cfg.json_head_items;
        budget.json_tail_items = fidelity_cfg.json_tail_items;
        budget.max_token_probe = fidelity_cfg.max_token_probe_per_message;
        return budget;
    };

    auto truncate_content = [&](const std::string& content, size_t max_len) -> std::string {
        if (content.length() <= max_len + cfg.min_compress_threshold) return content;

        CondenseBudget budget = make_condense_budget(content, max_len);
        CondenseResult condensed = ContentCondenser::Condense(content, ContentKind::kGenericText, budget, token_len_ptr);
        return condensed.text;
    };

    // JSON 感知的 tool 消息截断函数：tool 消息通常是 JSON 格式的搜索结果、文件内容或 API 响应。
    // ContentCondenser 内部会判断内容是否为 JSON 数组，命中则保留首 N + 尾 M 项（不再是旧版
    // "只保留前缀元素"的二分逻辑），未命中则回退到高信号行提取 + 头尾双保留的通用文本路径。
    auto truncate_tool_content = [&](const std::string& content, size_t max_len) -> std::string {
        if (content.length() <= max_len + cfg.min_compress_threshold) return content;

        CondenseBudget budget = make_condense_budget(content, max_len);
        CondenseResult condensed = ContentCondenser::Condense(content, ContentKind::kToolResponse, budget, token_len_ptr);
        return condensed.text;
    };

    // 初始 token 统计
    size_t initial_tokens = calc_msg_tokens(filtered_msg);
    My_Log{My_Log::Level::kInfo} << "[PreFilter] Budget: "
                                  << "sys_prompt=" << system_tokens << " tok"
                                  << ", msg_history=" << initial_tokens << " tok"
                                  << ", used=" << (system_tokens + initial_tokens) << " tok"
                                  << ", context=" << contextSize << " tok"
                                  << " (" << (contextSize > 0
                                        ? (int)(100.0 * (system_tokens + initial_tokens) / contextSize)
                                        : 0)
                                  << "% used)" << std::endl;

    bool budget_satisfied = false;

    // ── Phase 0: 无损清理（始终执行）──────────────────────────────────────────────
    // 冗余安全警告（SECURITY NOTICE: ... <<<EXTERNAL_UNTRUSTED_CONTENT>>>）是外部工具
    // 注入的格式噪声，无论预算是否充足，都应清理
    size_t after_phase0_tokens = initial_tokens;
    {
        size_t cleaned_count = 0;
        size_t metadata_filtered_count = 0;
        for (size_t i = 0; i < filtered_msg.size(); i++) {
            auto role = get_json_value(filtered_msg[i], "role", BLANK_STRING);
            if (role != "user") continue;
            std::string content = get_json_value(filtered_msg[i], "content", BLANK_STRING);
            std::string cleaned = CleanMessageContent(content);
            if (cleaned != content) {
                filtered_msg[i]["content"] = cleaned;
                metadata_filtered_count++;
            }
        }
        for (size_t i = 0; i < filtered_msg.size(); i++) {
            auto role = get_json_value(filtered_msg[i], "role", BLANK_STRING);
            if (role != "tool") continue;
            std::string content = get_json_value(filtered_msg[i], "content", BLANK_STRING);
            std::string cleaned = RemoveToolResponseRedundancy(content);
            if (cleaned.length() < content.length()) {
                filtered_msg[i]["content"] = cleaned;
                cleaned_count++;
            }
        }
        if (cleaned_count > 0) {
            after_phase0_tokens = calc_msg_tokens(filtered_msg);
            budget_satisfied = (after_phase0_tokens <= available_tokens);
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 0: Removed redundancy from "
                                               << cleaned_count << " tool messages" << std::endl;
                if (metadata_filtered_count > 0) {
                    My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 0: Filtered untrusted metadata from "
                                                   << metadata_filtered_count << " user messages" << std::endl;
                }
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 0: Tokens after cleanup: "
                                               << after_phase0_tokens << " tok"
                                               << " (was " << initial_tokens << " tok, saved "
                                               << (initial_tokens - after_phase0_tokens) << " tok)" << std::endl;
            }
        } else {
            if (metadata_filtered_count > 0) {
                after_phase0_tokens = calc_msg_tokens(filtered_msg);
                budget_satisfied = (after_phase0_tokens <= available_tokens);
                if (model_config_.getenablePromptDebug()) {
                    My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 0: Filtered untrusted metadata from "
                                                   << metadata_filtered_count << " user messages" << std::endl;
                    My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 0: Tokens after cleanup: "
                                                   << after_phase0_tokens << " tok"
                                                   << " (was " << initial_tokens << " tok, saved "
                                                   << (initial_tokens - after_phase0_tokens) << " tok)" << std::endl;
                }
            }
            if (model_config_.getenablePromptDebug()) {
                if (metadata_filtered_count == 0) {
                    My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 0: No redundancy found in tool messages or user metadata blocks" << std::endl;
                }
            }
            if (metadata_filtered_count == 0) {
                budget_satisfied = (initial_tokens <= available_tokens);
            }
        }
        if (budget_satisfied && model_config_.getenablePromptDebug()) {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 0: Within budget" << std::endl;
        }
    }

    if (prefix_cache_active && contextSize > 0) {
        const size_t cap_tokens = std::min(tool_entry_cap_tokens,
            static_cast<size_t>(static_cast<float>(contextSize) * kToolEntryHardCapRatio));
        size_t capped_count = 0;
        for (size_t i = 0; i < filtered_msg.size(); i++) {
            if (get_json_value(filtered_msg[i], "role", BLANK_STRING) != "tool") continue;
            std::string content = get_json_value(filtered_msg[i], "content", BLANK_STRING);
            if (content.size() <= cap_tokens) continue;
            std::string capped = CapToolResultHeadTail(content, CountTokens(content), cap_tokens);
            if (capped.size() < content.size()) {
                filtered_msg[i]["content"] = capped;
                capped_count++;
            }
        }
        if (capped_count > 0) {
            after_phase0_tokens = calc_msg_tokens(filtered_msg);
            budget_satisfied = (after_phase0_tokens <= available_tokens);
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Tool entry cap: capped=" << capped_count
                                          << ", cap=" << cap_tokens << " tok"
                                          << ", tokens=" << after_phase0_tokens << std::endl;
        }
    }

    // ── Phase 1: 旧消息处理（recent_window 之外）──────────────────────────
    // 按以下子步骤顺序执行，优先使用破坏性最小的手段：
    //   Step A：压缩旧 user/assistant 消息内容（old_compress_len）
    //   Step B：若仍超预算，删除旧消息（DropOldMessagesBatch）
    //   Step C：若仍超预算，压缩旧 tool 响应内容（old_compress_len）
    size_t compress_current_tokens = after_phase0_tokens;
    const float high_water_ratio = prefix_cache_active ? kEvictionHighWaterRatio : 1.0f;
    const float low_water_ratio = prefix_cache_active ? kEvictionLowWaterRatio : 1.0f;
    const size_t evict_high_water = static_cast<size_t>(
        static_cast<float>(available_tokens) * high_water_ratio);
    const size_t tokens_before_evict = compress_current_tokens;
    const size_t recent_growth = evict_key.empty() ? 0 :
        ObserveRecentGrowth(evict_key, handle.get(), msg.size(), compress_current_tokens);
    size_t evict_low_water = static_cast<size_t>(static_cast<float>(available_tokens) * low_water_ratio);
    if (prefix_cache_active && recent_growth > 0) {
        const size_t reserve = static_cast<size_t>(static_cast<float>(recent_growth) * kGrowthReserveRounds);
        const size_t floor_tokens = static_cast<size_t>(static_cast<float>(available_tokens) * kEvictionLowWaterFloorRatio);
        const size_t adaptive = evict_high_water > reserve ? evict_high_water - reserve : 0;
        evict_low_water = std::max(floor_tokens, std::min(evict_low_water, adaptive));
    }
    if ((!budget_satisfied || compress_current_tokens > evict_high_water) && !evict_key.empty()) {
        const MessageCategoryStats stats_before = prefilter_stats_;
        json working = filtered_msg;
        size_t evict_tokens = compress_current_tokens;
        current_prot_idx = compute_protection_indices(working);
        auto old_flags_ev = precompute_old_flags(working);
        DropOldMessagesBatch(working, old_flags_ev, is_protected,
                             handle, evict_tokens, evict_low_water,
                             [&]() { current_prot_idx = compute_protection_indices(working); });
        My_Log{My_Log::Level::kInfo} << "[PreFilter] Eviction low-water: target=" << evict_low_water
                                      << ", recent_growth=" << recent_growth
                                      << ", available=" << available_tokens << std::endl;
        evict_tokens = calc_msg_tokens(working);
        if (evict_tokens <= available_tokens) {
            filtered_msg = std::move(working);
            compress_current_tokens = evict_tokens;
            budget_satisfied = true;
        } else {
            prefilter_stats_ = stats_before;
        }
        current_prot_idx = compute_protection_indices(filtered_msg);
    }
    if (!budget_satisfied) {
        if (model_config_.getenablePromptDebug()) {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1: Processing old messages (recent_window="
                                           << cfg.recent_window << ")..." << std::endl;
        }

        // ── Phase 1 Step A: 压缩旧 user/assistant 消息内容 ──────────────
        {
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1 Step A: Compressing old user/assistant messages (max "
                                               << cfg.old_compress_len << " chars)..." << std::endl;
            }
            current_prot_idx = compute_protection_indices(filtered_msg);
            auto old_flags_1a = precompute_old_flags(filtered_msg);
            mask_repeat_protected(filtered_msg, old_flags_1a, false);
            size_t compressed_count = CompressMessages(
                filtered_msg, old_flags_1a, within_budget, is_truncate_protected, truncate_content,
                "user_assistant", /*only_old=*/true, cfg.old_compress_len,
                /*tool_min_length=*/0, "Phase 1A",
                handle, &compress_current_tokens, available_tokens,
                /*is_harmony=*/is_harmony);
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1 Step A: Compressed " << compressed_count << " messages" << std::endl;
            }
            budget_satisfied = (compress_current_tokens <= available_tokens);
            if (budget_satisfied && model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1 Step A: Within budget" << std::endl;
            }
        }

        // ── Phase 1 Step B: 删除旧消息（仍超预算时）────────────────────
        if (!budget_satisfied) {
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1 Step B: Dropping old messages (batch mode, low-water)..." << std::endl;
            }
            current_prot_idx = compute_protection_indices(filtered_msg);
            auto old_flags_1b = precompute_old_flags(filtered_msg);
            const size_t low_water_target_1b = static_cast<size_t>(
                static_cast<float>(available_tokens) * low_water_ratio);
            DropOldMessagesBatch(filtered_msg, old_flags_1b, is_protected,
                                 handle, compress_current_tokens, low_water_target_1b,
                                 [&]() { current_prot_idx = compute_protection_indices(filtered_msg); });
            budget_satisfied = (compress_current_tokens <= available_tokens);
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1 Step B: Tokens after drop: "
                                               << compress_current_tokens << " tok" << std::endl;
                if (budget_satisfied) {
                    My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1 Step B: Within budget" << std::endl;
                }
            }
        }

        // ── Phase 1 Step C: 压缩旧 tool 响应（仍超预算时）──────────────
        if (!budget_satisfied) {
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1 Step C: Compressing old tool responses (max="
                                               << cfg.old_compress_len << " chars)..." << std::endl;
            }
            current_prot_idx = compute_protection_indices(filtered_msg);
            auto old_flags_1c = precompute_old_flags(filtered_msg);
            mask_repeat_protected(filtered_msg, old_flags_1c, false);
            size_t compressed_count = CompressMessages(
                filtered_msg, old_flags_1c, within_budget, is_truncate_protected,
                truncate_tool_content,
                /*target_role=*/"tool", /*only_old=*/true,
                /*max_len=*/cfg.old_compress_len,
                /*tool_min_length=*/0, "Phase 1C",
                handle, &compress_current_tokens, available_tokens,
                /*is_harmony=*/is_harmony);
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1 Step C: Compressed " << compressed_count << " old tool messages" << std::endl;
            }
            budget_satisfied = (compress_current_tokens <= available_tokens);
            if (budget_satisfied && model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1 Step C: Within budget" << std::endl;
            }
        }

        if (model_config_.getenablePromptDebug()) {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1: Tokens after all steps: "
                                           << compress_current_tokens << " tok"
                                           << " (was " << initial_tokens << " tok, saved "
                                           << (initial_tokens > compress_current_tokens
                                               ? initial_tokens - compress_current_tokens : 0)
                                           << " tok)" << std::endl;
            if (!budget_satisfied) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 1: Still over budget, proceeding to Phase 2" << std::endl;
            }
        }
    }

    // ── Phase 2: 新消息处理（recent_window 之内）──────────────────────────
    // 按以下子步骤顺序执行：
    //   Step A：压缩新 user/assistant 消息内容（recent_compress_len）
    //   Step B：压缩新 tool 响应内容（tool_compress_len）
    if (!budget_satisfied) {
        if (model_config_.getenablePromptDebug()) {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 2: Processing recent messages..." << std::endl;
        }

        // ── Phase 2 Step A: 压缩新 user/assistant 消息内容 ──────────────
        {
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 2 Step A: Compressing recent user/assistant messages (max "
                                               << cfg.recent_compress_len << " chars)..." << std::endl;
            }
            current_prot_idx = compute_protection_indices(filtered_msg);
            auto old_flags_2a = precompute_old_flags(filtered_msg);
            mask_repeat_protected(filtered_msg, old_flags_2a, true);
            size_t compressed_count = CompressMessages(
                filtered_msg, old_flags_2a, within_budget, is_truncate_protected, truncate_content,
                "user_assistant", /*only_old=*/false, cfg.recent_compress_len,
                /*tool_min_length=*/0, "Phase 2A",
                handle, &compress_current_tokens, available_tokens,
                /*is_harmony=*/is_harmony);
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 2 Step A: Compressed " << compressed_count << " messages" << std::endl;
            }
            budget_satisfied = (compress_current_tokens <= available_tokens);
            if (budget_satisfied && model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 2 Step A: Within budget" << std::endl;
            }
        }

        // ── Phase 2 Step B: 压缩新 tool 响应（仍超预算时）──────────────
        if (!budget_satisfied) {
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 2 Step B: Compressing recent tool responses (max="
                                               << cfg.tool_compress_len << " chars)..." << std::endl;
            }
            current_prot_idx = compute_protection_indices(filtered_msg);
            auto old_flags_2b = precompute_old_flags(filtered_msg);
            mask_repeat_protected(filtered_msg, old_flags_2b, true);
            size_t compressed_count = CompressMessages(
                filtered_msg, old_flags_2b, within_budget, is_truncate_protected,
                truncate_tool_content,
                /*target_role=*/"tool", /*only_old=*/false,
                /*max_len=*/cfg.tool_compress_len,
                /*tool_min_length=*/cfg.tool_min_length, "Phase 2B",
                handle, &compress_current_tokens, available_tokens,
                /*is_harmony=*/is_harmony);
            if (model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 2 Step B: Compressed " << compressed_count << " recent tool messages" << std::endl;
            }
            budget_satisfied = (compress_current_tokens <= available_tokens);
            if (budget_satisfied && model_config_.getenablePromptDebug()) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 2 Step B: Within budget" << std::endl;
            }
        }
    }

    // ── Phase 3: 丢弃新消息（最后手段，跳过保护集）──────────────────────
    if (!budget_satisfied) {
        if (model_config_.getenablePromptDebug()) {
            My_Log{My_Log::Level::kWarning} << "[PreFilter] Phase 3: Dropping recent messages (last resort, batch mode)..." << std::endl;
        }
        current_prot_idx = compute_protection_indices(filtered_msg);
        std::vector<bool> all_flags(filtered_msg.size(), true);
        const size_t low_water_target_p3 = static_cast<size_t>(
            static_cast<float>(available_tokens) * low_water_ratio);
        DropOldMessagesBatch(filtered_msg, all_flags, is_protected,
                             handle, compress_current_tokens, low_water_target_p3,
                             [&]() { current_prot_idx = compute_protection_indices(filtered_msg); });
        budget_satisfied = (compress_current_tokens <= available_tokens);
        if (model_config_.getenablePromptDebug()) {
            if (budget_satisfied) {
                My_Log{My_Log::Level::kInfo} << "[PreFilter] Phase 3: Within budget" << std::endl;
            } else {
                My_Log{My_Log::Level::kWarning} << "[PreFilter] Phase 3: Still over budget, FitMessagesToContext will handle remainder" << std::endl;
            }
        }
    }

    // ── 最终统计 ──────────────────────────────────────────────────────────
    {
        size_t final_tokens = compress_current_tokens;
        size_t total_after = system_tokens + final_tokens;
        My_Log{My_Log::Level::kInfo} << "[PreFilter] Final: "
                                      << "msg_history=" << final_tokens << " tok"
                                      << ", used=" << total_after << "/" << contextSize << " tok"
                                      << " (" << (contextSize > 0
                                            ? (int)(100.0 * total_after / contextSize)
                                            : 0)
                                      << "% used)"
                                      << ", msgs=" << filtered_msg.size() << std::endl;
    }

    // ── 步骤 4: 验证工具调用序列的完整性 ──────────────────────────────────
    json validated_msg = ValidateToolCallSequence(filtered_msg);
    if (model_config_.getenablePromptDebug()) {
        if (validated_msg.size() < filtered_msg.size()) {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Step 4: Removed "
                                           << (filtered_msg.size() - validated_msg.size())
                                           << " orphaned tool messages" << std::endl;
        } else {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Step 4: Tool call sequence validation passed" << std::endl;
        }
    }

    // ── 步骤 5: 最终验证 - 确保至少有一条 user 消息 ──────────────────────
    bool has_final_user = false;
    for (const auto& element : validated_msg) {
        if (element["role"] == "user") { has_final_user = true; break; }
    }

    if (!has_final_user) {
        My_Log{My_Log::Level::kError} << "[PreFilter] Step 5: CRITICAL - No user message in final result!" << std::endl;
        for (int i = (int)msg.size() - 1; i >= 0; i--) {
            if (msg[i]["role"] == "user") {
                size_t insert_pos = 0;
                if (!validated_msg.empty() && validated_msg[0]["role"] == "system") {
                    insert_pos = 1;
                }
                validated_msg.insert(validated_msg.begin() + insert_pos, raw_tagged[i]);
                My_Log{My_Log::Level::kWarning} << "[PreFilter] Step 5: Force added last user message" << std::endl;
                break;
            }
        }
    } else {
        if (model_config_.getenablePromptDebug()) {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Step 5: Final validation passed" << std::endl;
        }
    }

    // ── [DIAG] 打印最终压缩后的完整内容（system prompt + 所有 messages）──────
    // 触发条件：仍超出预算（budget_satisfied == false），即将进入 FitMessagesToContext 处理
    // 目的：帮助分析哪部分内容占用 tokens 较多，用于决策进一步删减和压缩
    if (!budget_satisfied) {
        print_diag_dump(validated_msg);
    }

    {
        std::vector<bool> present(msg.size(), false);
        for (const auto& m : validated_msg) {
            if (m.is_object() && m.contains(kRawIdxKey)) {
                const size_t ri = m[kRawIdxKey].get<size_t>();
                if (ri < present.size()) present[ri] = true;
            }
        }
        EvictionState next_state;
        next_state.model = handle.get();
        next_state.raw_size = msg.size();
        for (size_t i = 0; i < present.size(); ++i) {
            if (!present[i]) next_state.dropped.push_back(i);
        }
        next_state.boundary = next_state.dropped.empty() ? 0 : next_state.dropped.back() + 1;
        next_state.prefix_hash = HashRawPrefix(msg_raw_for_hash, next_state.boundary);
        const size_t prior_count = have_prior ? prior_state.dropped.size() : 0;
        std::string reset_reason = "none";
        if (evict_key.empty()) {
            if (!next_state.dropped.empty()) reset_reason = "untracked_drop";
        } else if (have_prior) {
            if (next_state.dropped.size() > prior_count) reset_reason = "forced_budget";
        } else if (!next_state.dropped.empty()) {
            reset_reason = invalid_reason;
        }
        prefilter_stats_.messages_dropped_prefix_len = next_state.dropped.size();
        prefilter_stats_.prefix_reset_reason = reset_reason;
        if (!next_state.dropped.empty() || have_prior) {
            My_Log{My_Log::Level::kInfo} << "[PreFilter] Eviction: sticky=" << prior_count
                                          << ", dropped=" << next_state.dropped.size()
                                          << ", boundary=" << next_state.boundary
                                          << ", tokens=" << compress_current_tokens
                                          << ", reset_reason=" << reset_reason << std::endl;
        }
        if (!evict_key.empty()) {
            StoreEvictionState(evict_key, std::move(next_state));
            StoreRoundTokens(evict_key, handle.get(), msg.size(), tokens_before_evict, compress_current_tokens);
        }
    }

    CaptureDroppedMessages(msg, validated_msg);
    StripRawIdx(validated_msg);
    return validated_msg;
}
