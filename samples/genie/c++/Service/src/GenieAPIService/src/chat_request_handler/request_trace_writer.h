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

#include "prompt_ledger.h"
#include "../model/model_config.h"

using json = nlohmann::ordered_json;

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
    };

    void WriteRecord(const Record &rec)
    {
        if (!enabled_)
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
        j["request_body"] = json::parse(rec.request_body, nullptr, false);

        std::lock_guard<std::mutex> lk(mutex_);
        if (!ofs_.is_open())
        {
            return;
        }
        ofs_ << j.dump() << "\n";
        ofs_.flush();
    }

private:
    RequestTraceWriter() = default;

    std::mutex mutex_;
    std::ofstream ofs_;
    std::atomic<bool> enabled_{false};
};

#endif //REQUEST_TRACE_WRITER_H
