//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef REQUEST_TRACE_WRITER_H
#define REQUEST_TRACE_WRITER_H

#include <nlohmann/json.hpp>
#include <fstream>
#include <mutex>
#include <atomic>
#include <string>
#include <chrono>
#include <filesystem>
#include <functional>
#include <sstream>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#include <algorithm>

#include "prompt_ledger.h"
#include "../model/model_config.h"
#include "task_memo_builder.h"

using json = nlohmann::ordered_json;

namespace request_trace_shape {

inline constexpr size_t kPayloadMinBytes = 24;
inline constexpr size_t kResourceMaxBytes = 1024;

inline constexpr const char *kRangeKeys[] = {"offset", "limit", "start_line", "end_line", "line_start",
                                             "line_end", "startLine", "endLine", "lines", "line_range", "range"};

inline bool IsResourceValue(const std::string &v)
{
    if (v.empty() || v.size() > kResourceMaxBytes)
    {
        return false;
    }
    if (v.find_first_of(" \t\r\n") != std::string::npos)
    {
        return false;
    }
    return v.find("://") != std::string::npos || v.find('/') != std::string::npos ||
           v.find('\\') != std::string::npos;
}

inline void CollectStringValues(const json &v, std::vector<std::string> &out)
{
    if (v.is_string())
    {
        out.push_back(v.get<std::string>());
        return;
    }
    if (v.is_array() || v.is_object())
    {
        for (const auto &child : v)
        {
            CollectStringValues(child, out);
        }
    }
}

inline json ParseArguments(const json &args)
{
    if (args.is_string())
    {
        json parsed = json::parse(args.get<std::string>(), nullptr, false);
        return parsed.is_discarded() ? json(nullptr) : parsed;
    }
    return args;
}

struct CallShape {
    std::string kind = "exec";
    std::string resource;
    json offset = nullptr;
    json limit = nullptr;
    std::string range_sig;
};

inline CallShape ClassifyCall(const json &function_obj)
{
    CallShape shape;
    if (!function_obj.is_object() || !function_obj.contains("arguments"))
    {
        return shape;
    }
    const json args = ParseArguments(function_obj.at("arguments"));
    if (!args.is_object())
    {
        return shape;
    }

    std::string resource_key;
    for (auto it = args.begin(); it != args.end(); ++it)
    {
        if (it.value().is_string() && IsResourceValue(it.value().get<std::string>()))
        {
            resource_key = it.key();
            shape.resource = it.value().get<std::string>();
            break;
        }
    }
    if (shape.resource.empty())
    {
        return shape;
    }

    size_t payload_count = 0, payload_max = 0;
    for (auto it = args.begin(); it != args.end(); ++it)
    {
        if (it.key() == resource_key)
        {
            continue;
        }
        std::vector<std::string> values;
        CollectStringValues(it.value(), values);
        for (const std::string &v : values)
        {
            if (v.empty())
            {
                continue;
            }
            payload_count++;
            payload_max = std::max(payload_max, v.size());
        }
    }
    shape.kind = (payload_max >= kPayloadMinBytes || payload_count >= 2) ? "write" : "read";

    std::ostringstream sig;
    for (const char *k : kRangeKeys)
    {
        if (args.contains(k) && !args.at(k).is_null())
        {
            sig << k << '=' << args.at(k).dump() << ';';
        }
    }
    shape.range_sig = sig.str();
    if (args.contains("offset") && args.at("offset").is_number())
    {
        shape.offset = args.at("offset");
    }
    if (args.contains("limit") && args.at("limit").is_number())
    {
        shape.limit = args.at("limit");
    }
    return shape;
}

inline void AnalyzeRound(const json &body, json &read_sources, json &reread)
{
    read_sources = json::array();
    reread = json::object();
    size_t after_write = 0, detail = 0, evicted = 0;
    json items = json::array();

    if (body.is_object() && body.contains("messages") && body.at("messages").is_array())
    {
        struct ReadMark {
            size_t call_index;
            std::unordered_set<std::string> ranges;
        };
        std::unordered_map<std::string, ReadMark> last_read;
        std::unordered_map<std::string, size_t> last_write;
        size_t call_index = 0, msg_index = 0;

        for (const auto &msg : body.at("messages"))
        {
            const size_t this_msg = msg_index++;
            if (!msg.is_object() || !msg.contains("tool_calls") || !msg.at("tool_calls").is_array())
            {
                continue;
            }
            for (const auto &tc : msg.at("tool_calls"))
            {
                if (!tc.is_object() || !tc.contains("function"))
                {
                    continue;
                }
                const size_t this_call = call_index++;
                const CallShape shape = ClassifyCall(tc.at("function"));
                if (shape.kind == "write")
                {
                    last_write[shape.resource] = this_call;
                    continue;
                }
                if (shape.kind != "read")
                {
                    continue;
                }

                json src;
                src["call_index"] = this_call;
                src["msg_index"] = this_msg;
                src["resource"] = shape.resource;
                src["offset"] = shape.offset;
                src["limit"] = shape.limit;
                read_sources.push_back(std::move(src));

                auto prev = last_read.find(shape.resource);
                if (prev != last_read.end())
                {
                    auto w = last_write.find(shape.resource);
                    std::string cls;
                    if (w != last_write.end() && w->second > prev->second.call_index)
                    {
                        cls = "after_write";
                        after_write++;
                    }
                    else if (prev->second.ranges.count(shape.range_sig) > 0)
                    {
                        cls = "evicted";
                        evicted++;
                    }
                    else
                    {
                        cls = "detail";
                        detail++;
                    }
                    json item;
                    item["resource"] = shape.resource;
                    item["class"] = cls;
                    item["call_index"] = this_call;
                    item["prev_call_index"] = prev->second.call_index;
                    items.push_back(std::move(item));
                    prev->second.call_index = this_call;
                    prev->second.ranges.insert(shape.range_sig);
                }
                else
                {
                    ReadMark mark;
                    mark.call_index = this_call;
                    mark.ranges.insert(shape.range_sig);
                    last_read.emplace(shape.resource, std::move(mark));
                }
            }
        }
    }

    reread["reread"] = !items.empty();
    reread["after_write"] = after_write;
    reread["detail"] = detail;
    reread["evicted"] = evicted;
    reread["items"] = std::move(items);
}

inline size_t CommonPrefixBytes(const std::string &a, const std::string &b)
{
    const size_t n = std::min(a.size(), b.size());
    size_t i = 0;
    while (i < n && a[i] == b[i])
    {
        i++;
    }
    auto mid_codepoint = [](const std::string &s, size_t pos) {
        return pos < s.size() && (static_cast<unsigned char>(s[pos]) & 0xC0) == 0x80;
    };
    while (i > 0 && (mid_codepoint(a, i) || mid_codepoint(b, i)))
    {
        i--;
    }
    return i;
}

}

class RequestTraceWriter {
public:
    static RequestTraceWriter &GetInstance()
    {
        static RequestTraceWriter instance;
        return instance;
    }

    void Configure(const RequestTraceConfig &cfg)
    {
        std::lock_guard<std::mutex> lk(mutex_);
        enabled_ = cfg.enabled;
        if (ofs_.is_open())
        {
            ofs_.close();
        }
        if (!enabled_)
        {
            return;
        }

        std::filesystem::path p(cfg.path);
        if (p.has_parent_path())
        {
            std::error_code ec;
            std::filesystem::create_directories(p.parent_path(), ec);
        }
        ofs_.open(p, std::ios::out | std::ios::app | std::ios::binary);
    }

    bool IsEnabled() const
    {
        return enabled_;
    }

    struct Record {
        std::string request_id;
        std::string model_name;
        std::string final_prompt;
        std::string raw_output;
        json tool_calls = nullptr;
        bool is_stream = false;
        std::string finish_reason;
        json backend_metrics = nullptr;
        json prompt_ledger = nullptr;
        std::string request_body;
        std::function<size_t(const std::string &)> token_length;
    };

    void WriteRecord(const Record &rec)
    {
        if (!enabled_)
        {
            return;
        }

        std::lock_guard<std::mutex> lk(mutex_);
        if (!ofs_.is_open())
        {
            return;
        }

        auto now = std::chrono::system_clock::now();
        auto now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
            now.time_since_epoch()).count();

        json j;
        j["request_id"] = rec.request_id;
        j["timestamp_ms"] = now_ms;
        j["model"] = rec.model_name;
        j["is_stream"] = rec.is_stream;
        j["finish_reason"] = rec.finish_reason;
        j["final_prompt"] = rec.final_prompt;
        j["raw_output"] = rec.raw_output;
        j["tool_calls"] = rec.tool_calls;
        j["prompt_ledger"] = rec.prompt_ledger;
        j["messages_dropped_prefix_len"] = (rec.prompt_ledger.is_object() && rec.prompt_ledger.contains("messages_dropped_prefix_len"))
            ? rec.prompt_ledger.at("messages_dropped_prefix_len") : json(nullptr);
        j["prefix_reset_reason"] = (rec.prompt_ledger.is_object() && rec.prompt_ledger.contains("prefix_reset_reason"))
            ? rec.prompt_ledger.at("prefix_reset_reason") : json(nullptr);

        const json body = json::parse(rec.request_body, nullptr, false);
        json read_sources, reread;
        request_trace_shape::AnalyzeRound(body, read_sources, reread);
        j["read_sources"] = std::move(read_sources);
        j["reread_class"] = std::move(reread);

        std::string session_bucket = rec.model_name;
        if (body.is_object() && body.contains("messages"))
        {
            session_bucket += "|" + TaskMemoBuilder::ComputeFirstMsgKey(body.at("messages"));
        }
        std::string &prev = prev_prompt_[session_bucket];
        size_t lcp_tokens = 0, recomputed_tokens = 0;
        if (rec.token_length)
        {
            const size_t lcp_bytes = request_trace_shape::CommonPrefixBytes(prev, rec.final_prompt);
            lcp_tokens = rec.token_length(rec.final_prompt.substr(0, lcp_bytes));
            const size_t total_tokens = rec.token_length(rec.final_prompt);
            recomputed_tokens = total_tokens > lcp_tokens ? total_tokens - lcp_tokens : 0;
        }
        prev = rec.final_prompt;
        j["lcp_tokens"] = lcp_tokens;
        j["recomputed_tokens"] = recomputed_tokens;

        const json &m = rec.backend_metrics;
        auto field = [&m](const char *key) -> json {
            return (m.is_object() && m.contains(key)) ? m.at(key) : json(nullptr);
        };
        j["prompt_tokens"] = field("prompt_tokens");
        j["gen_tokens"] = field("gen_tokens");
        j["prefix_reused_tokens"] = field("prefix_reused_tokens");
        j["aux_ms"] = field("aux_ms");
        j["aux_calls"] = field("aux_calls");
        j["ttft_ms"] = field("ttft_ms");
        j["prefill_ms"] = field("prefill_ms");
        j["prefill_tok_s"] = field("prefill_tok_s");
        j["decode_ms"] = field("decode_ms");
        j["decode_tok_s"] = field("decode_tok_s");
        j["draft_proposed"] = field("draft_proposed");
        j["draft_accepted"] = field("draft_accepted");
        j["draft_acceptance_rate"] = field("draft_acceptance_rate");
        j["draft_accept_hist"] = field("draft_accept_hist");
        j["spec_rounds"] = field("spec_rounds");
        j["spec_draft_ms"] = field("spec_draft_ms");
        j["spec_verify_ms"] = field("spec_verify_ms");
        j["spec_readback_ms"] = field("spec_readback_ms");
        j["spec_post_ms"] = field("spec_post_ms");
        j["request_body"] = body;

        ofs_ << j.dump() << "\n";
        ofs_.flush();
    }

private:
    RequestTraceWriter() = default;

    std::mutex mutex_;
    std::ofstream ofs_;
    std::atomic<bool> enabled_{false};
    std::unordered_map<std::string, std::string> prev_prompt_;
};

#endif
