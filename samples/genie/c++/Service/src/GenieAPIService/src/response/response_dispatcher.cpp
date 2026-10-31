//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "response_dispatcher.h"
#include "../chat_request_handler/model_input_builder.h"
#include "response_tools.h"
#include "mimicry_recovery.h"
#include "repeat_gate.h"

#include "log.h"
#include "../processor/general.h"
#include "../processor/harmony.h"
#include "watermark_provider_host.h"
#include <regex>
#include <algorithm>
#include <chrono>
#include <mutex>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace
{
    constexpr char kMimicryAbortErrMsg[] =
        R"({"error": {"message": "Generation aborted after runaway reasoning-note mimicry without producing a tool call.", "type": "server_error", "code": 500}})";

    bool IsUnknowToolCallJson(const std::string &converted_json_str)
    {
        try
        {
            json obj = json::parse(ResponseTools::extractJsonFromToolCall(converted_json_str));
            return obj.contains("name") && obj["name"].is_string()
                   && obj["name"].get<std::string>() == "unknow";
        }
        catch (...)
        {
            return true;
        }
    }

    bool IsFailureReasonSkipped(ToolCallFailureReason reason, const std::vector<std::string> &skip_reasons)
    {
        const std::string reason_str = ResponseTools::ToolCallFailureReasonToString(reason);
        return std::find(skip_reasons.begin(), skip_reasons.end(), reason_str) != skip_reasons.end();
    }

    std::string BuildToolRepairErrorMessage(ToolCallFailureReason reason)
    {
        auto join_known_tool_names = []() -> std::string
        {
            std::string joined;
            for (const auto &kv : PromptOptimizer::GetKnownToolSignatures())
            {
                if (!joined.empty()) joined += ", ";
                joined += kv.first;
            }
            return joined;
        };

        switch (reason)
        {
            case ToolCallFailureReason::kUnknownToolName:
                return "Error: unknown tool name. Available tools: " + join_known_tool_names() +
                       ". Please retry with one of these exact tool names in a single valid JSON object.";
            case ToolCallFailureReason::kMissingRequiredArgs:
                return "Error: missing required argument(s) for the tool you tried to call. "
                       "Please retry with the exact tool name and ALL of its required arguments "
                       "included in a single valid JSON object.";
            case ToolCallFailureReason::kAmbiguousMultipleCalls:
                return "Error: your response contained multiple ambiguous tool call candidates. "
                       "Please output exactly one single valid JSON tool_call object.";
            case ToolCallFailureReason::kTruncated:
                return "Error: your tool call output appears to have been truncated. "
                       "Please retry with a shorter, complete, single valid JSON object.";
            case ToolCallFailureReason::kUnparseable:
            case ToolCallFailureReason::kNone:
            default:
                return "Error: invalid tool call format, could not parse as JSON. "
                       "Please retry with a single valid JSON object, no extra text.";
        }
    }

    std::string TailBytes(const std::string &s, size_t max_bytes)
    {
        if (s.size() <= max_bytes) return s;
        size_t start = s.size() - max_bytes;
        while (start < s.size() && (static_cast<unsigned char>(s[start]) & 0xC0) == 0x80) ++start;
        return s.substr(start);
    }
}

ResponseDispatcher::ResponseDispatcher(IModelConfig &model_mgr,
                                       ChatHistory &chatHistory,
                                       const ModelInstanceConfig *instance_config) :
        chatHistory(chatHistory),
        model_config_(model_mgr),
        instance_config_(instance_config)
{
    ResetProcessor();
}

void ResponseDispatcher::ResetProcessor()
{
    if (proc_)
    {
        delete proc_;
        proc_ = nullptr;
    }

    PromptType prompt_type = instance_config_
        ? instance_config_->get_prompt_type()
        : model_config_.get_prompt_type();

    switch (int(prompt_type))
    {
        case PromptType::Harmony:
            proc_ = new HarmonyProcessor{};
            break;
        default:
            proc_ = new GeneralProcessor{};
    }

    chatHistory.Clear();
}

void ResponseDispatcher::Prepare(ModelInput &model_input,
                                 bool is_tool,
                                 bool is_stream,
                                 const httplib::Request &req,
                                 bool is_dll_mode,
                                 const json *request_data_for_retry)
{
    if (is_dll_mode)
        this->req_ = nullptr;
    else
        this->req_ = &const_cast<httplib::Request &>(req);

    this->model_input_ = model_input;
    is_stream_ = is_stream;
    is_tool_ = is_tool;
    stream_write_failed_.store(false);
    sink_write_wrapped_.store(false);

    this->retry_request_data_ = request_data_for_retry ? *request_data_for_retry : json();
    proc_->Clean();

    status_tool_call_sent_ = false;
    status_code_sent_ = false;

}

void ResponseDispatcher::SendStatusUpdate(httplib::DataSink *sink,
                                          const std::string &status,
                                          const std::string &message)
{
    if (!is_stream_ || !sink)
        return;
    ResponseTools::post_stream_data(*sink, "data",
        ResponseTools::statusDataJson(status, message));
    My_Log{} << "[Status] " << status << ": " << message << std::endl;
}

void ResponseDispatcher::SendKeepAlive(httplib::DataSink *sink)
{
    if (!is_stream_ || !sink)
        return;

    ResponseTools::post_stream_data(*sink, "data",
        ResponseTools::responseDataJson("", "", true));
    My_Log{My_Log::Level::kDebug} << "[KeepAlive] sent empty delta frame" << std::endl;
}

bool ResponseDispatcher::SendResponse(size_t, httplib::DataSink *sink, httplib::Response *res, bool suppress_end_on_overflow)
{
    if (is_stream_ && sink && !sink_write_wrapped_.exchange(true))
    {
        auto original_write = sink->write;
        sink->write = [this, original_write](const char *data, size_t data_length)
        {
            const bool written = original_write(data, data_length);
            if (!written && !stream_write_failed_.exchange(true))
            {
                My_Log{My_Log::Level::kWarning}
                    << "[SendResponse] Client stream write failed; stopping inference.\n";
            }
            return written;
        };
    }

    auto handle = GetEffectiveHandle();
    if (!handle)
    {
        My_Log{My_Log::Level::kError}
            << "[SendResponse] Model handle is null (model may have been unloaded or reset). "
            << "Aborting request instead of crashing." << std::endl;
        constexpr char *null_handle_err =
            R"({"error": {"message": "Model handle unavailable, the model may have been unloaded or reset.", "type": "server_error", "code": 500}})";
        if (is_stream_ && sink)
        {
            ResponseTools::post_stream_data(*sink, "error", null_handle_err, false);
            ResponseTools::post_stream_data(*sink, "data", "[DONE]", true);
        }
        else if (res)
        {
            res->status = 500;
            res->set_content(null_handle_err, MIMETYPE_JSON);
        }
        return false;
    }
    std::string toolResponse;
    std::string finishReason = "stop";
    response_buffer.clear();
    bool isToolResponse = false;

    bool connection_broken = false;
    bool degenerate_mimicry_stop = false;
    size_t mimicry_tail_tokens = 0;
    bool mimicry_soft_stop = false;
    bool mimicry_soft_retry_used = false;

    bool extra_retry_used = false;

    std::string repeat_block_body;

    bool layer2_retry_rolled_back = false;
    bool has_harmony_history_snapshot = false;
    std::string snapshot_harmony_history_msg;
    bool snapshot_harmony_is_tool_call = false;
    auto capture_harmony_history_snapshot = [&]()
    {
        if (GetEffectivePromptType() != PromptType::Harmony)
        {
            return;
        }
        auto *harmony_proc_before_retry = dynamic_cast<HarmonyProcessor *>(proc_);
        if (harmony_proc_before_retry)
        {
            snapshot_harmony_history_msg = harmony_proc_before_retry->GetMessageForHistory();
            snapshot_harmony_is_tool_call = harmony_proc_before_retry->IsToolCall();
            has_harmony_history_snapshot = true;
        }
    };

    ResponseTools::BareJsonHoldState bare_json_state;
    bool harmony_thinking_status_sent = false;
    auto last_hold_keepalive_at = std::chrono::steady_clock::now();

    auto genie_callback = [&](std::string &message)
    {

        if (!isConnectionAlive())
        {

            connection_broken = true;
            return false;
        }

        if (message.empty())
        {
            SendKeepAlive(sink);
            return true;
        }

#ifndef GENIEAPI_EXPORTS
        My_Log{}.original(true) << message;
#endif
        std::string chunk = message;

        auto result = preprocessStream(chunk, isToolResponse, toolResponse);
        isToolResponse = std::get<0>(result);

        if (GetEffectivePromptType() == PromptType::General)
        {

            chunk = std::get<1>(result);
        }

        if (is_stream_ && sink && GetEffectivePromptType() == PromptType::Harmony)
        {
            const std::string reasoning = proc_->TakeReasoningDelta();
            if (!reasoning.empty())
            {
                ResponseTools::post_stream_data(*sink, "data", ResponseTools::reasoningDataJson(reasoning));
                last_hold_keepalive_at = std::chrono::steady_clock::now();
            }
        }

        if (is_stream_ && sink && GetEffectivePromptType() == PromptType::Harmony &&
            chunk.empty() && !isToolResponse)
        {
            const auto now = std::chrono::steady_clock::now();
            if (!harmony_thinking_status_sent)
            {
                SendStatusUpdate(sink, "thinking", "Thinking...");
                harmony_thinking_status_sent = true;
                last_hold_keepalive_at = now;
            }
            else if (std::chrono::duration_cast<std::chrono::seconds>(
                         now - last_hold_keepalive_at).count() >= 5)
            {
                SendKeepAlive(sink);
                last_hold_keepalive_at = now;
            }
        }

        response_buffer += message;

        if (is_tool_ && TaskMemoBuilder::IsReasoningNoteMimicry(response_buffer))
        {
            ++mimicry_tail_tokens;
            const auto mimicry_action = MimicryRecovery::Decide(
                mimicry_tail_tokens, mimicry_soft_retry_used, response_buffer.find("<tool_call>") != std::string::npos);
            if (mimicry_action == MimicryRecovery::Action::kSoftStop)
            {
                mimicry_soft_stop = true;
                My_Log{My_Log::Level::kWarning}
                    << "[SendResponse] Reasoning-note mimicry passed soft limit after "
                    << mimicry_tail_tokens << " callback chunks; stopping for one soft-landing retry." << std::endl;
                return false;
            }
            if (mimicry_action == MimicryRecovery::Action::kHardStop)
            {
                degenerate_mimicry_stop = true;
                My_Log{My_Log::Level::kWarning}
                    << "[SendResponse] Aborting runaway reasoning-note mimicry without a tool call after "
                    << mimicry_tail_tokens << " callback chunks." << std::endl;
                return false;
            }
        }

        if (is_tool_ && !isToolResponse && GetEffectivePromptType() == PromptType::General)
        {
            ResponseTools::ApplyBareJsonHoldBack(bare_json_state, response_buffer, message, chunk);

            if (bare_json_state.bare_json_hold)
            {

                auto now = std::chrono::steady_clock::now();
                if (std::chrono::duration_cast<std::chrono::seconds>(
                        now - last_hold_keepalive_at).count() >= 5)
                {
                    SendKeepAlive(sink);
                    last_hold_keepalive_at = now;
                }
            }
        }

        if (is_stream_ && sink)
        {

            if (!status_code_sent_ && message.find("```") != std::string::npos)
            {
                SendStatusUpdate(sink, "writing_code", "Writing code...");
                status_code_sent_ = true;
            }
        }

        bool content_sent = false;

        if (!chunk.empty())
        {

            if (is_tool_ && isToolResponse)
            {

                My_Log{}.original(true) << "\n";
                My_Log{} << "[Stream] Tool call detected, suppressing output" << std::endl;
                content_sent = true;
            }
            else
            {

                if (is_stream_ && sink)
                {
                    ResponseTools::post_stream_data(*sink, "data",
                        ResponseTools::responseDataJson(chunk, "", true));
                }
                content_sent = true;
            }
        }

        if (is_tool_ && isToolResponse && !GetEffectiveIsOutputAllText())
            return true;

        if (is_stream_ && sink && !content_sent && !chunk.empty())
            ResponseTools::post_stream_data(*sink, "data",
                ResponseTools::responseDataJson(chunk, "", true));
        return true;
    };

    try
    {
        My_Log{}.original(true) << "\n";

        SendStatusUpdate(sink, "inference", "Inferencing...");

        ContextBase::PrefillHeartbeatCallback prefill_heartbeat = nullptr;
        if ((is_stream_ && sink) || (!is_stream_ && req_))
        {
            prefill_heartbeat = [&]() -> bool
            {
                if (!isConnectionAlive())
                {
                    My_Log{My_Log::Level::kWarning}
                        << "[SendResponse] Prefill heartbeat: connection broken, aborting prefill.\n";
                    connection_broken = true;
                    return false;
                }
                if (is_stream_ && sink)
                {
                    SendKeepAlive(sink);
                    if (!isConnectionAlive())
                    {
                        connection_broken = true;
                        return false;
                    }
                }
                return true;
            };
        }

        My_Log{} << "--- Query Context Start ---" << std::endl;
        bool first_query_ok = handle->Query(model_input_, genie_callback, prefill_heartbeat);
        if (!first_query_ok && mimicry_soft_stop && !mimicry_soft_retry_used && !connection_broken
            && instance_config_ && !retry_request_data_.is_null()
            && retry_request_data_.contains("messages") && retry_request_data_["messages"].is_array()
            && RepeatGateLogic::TryConsumeExtraRetry(extra_retry_used))
        {
            mimicry_soft_retry_used = true;
            try
            {
                json scratch_data = retry_request_data_;
                json assistant_msg;
                assistant_msg["role"] = "assistant";
                assistant_msg["content"] = response_buffer;
                json correction_msg;
                correction_msg["role"] = "user";
                correction_msg["content"] = MimicryRecovery::BuildCorrectionMessage();
                scratch_data["messages"].push_back(assistant_msg);
                scratch_data["messages"].push_back(correction_msg);

                auto *mutable_instance_config = const_cast<ModelInstanceConfig *>(instance_config_);
                ChatHistory scratch_chat_history(*mutable_instance_config);
                ModelInputBuilder scratch_builder(scratch_chat_history, mutable_instance_config, scratch_data);

                bool scratch_is_tool = false;
                std::function<bool()> scratch_is_alive_fn = nullptr;
                if (is_stream_ && sink)
                {
                    scratch_is_alive_fn = [sink]() -> bool { return sink->is_writable(); };
                }
                ModelInput scratch_model_input =
                    scratch_builder.Build(scratch_data, scratch_is_tool, scratch_is_alive_fn);

                response_buffer.clear();
                toolResponse.clear();
                isToolResponse = false;
                degenerate_mimicry_stop = false;
                mimicry_soft_stop = false;
                mimicry_tail_tokens = 0;
                proc_->Clean();
                is_tool_ = scratch_is_tool;
                bare_json_state = ResponseTools::BareJsonHoldState();
                harmony_thinking_status_sent = false;
                last_hold_keepalive_at = std::chrono::steady_clock::now();

                My_Log{} << "--- Query Context Start (mimicry soft-landing retry) ---" << std::endl;
                first_query_ok = handle->Query(scratch_model_input, genie_callback, prefill_heartbeat);
                My_Log{} << "--- Query Context End (mimicry soft-landing retry) ---\n" << std::endl;
            }
            catch (const std::exception &e)
            {
                My_Log{My_Log::Level::kError}
                    << "[SendResponse] Exception during mimicry soft-landing retry: " << e.what() << std::endl;
                first_query_ok = false;
            }
        }
        bool mimicry_degraded = false;
        if (MimicryRecovery::ShouldDegradeInsteadOfAbort(first_query_ok, degenerate_mimicry_stop, connection_broken,
                                                         response_buffer.size()))
        {
            mimicry_degraded = true;
            first_query_ok = true;
            My_Log{My_Log::Level::kWarning}
                << "[SendResponse] Mimicry hard limit reached; degrading to finish_reason=length. Tail: "
                << response_buffer.substr(response_buffer.size() > 200 ? response_buffer.size() - 200 : 0) << std::endl;
        }
        if (!first_query_ok)
        {

            const char *err = degenerate_mimicry_stop
                ? kMimicryAbortErrMsg
                : R"({"error": {"message": "Model query unavailable or generation failed internally.", "type": "server_error", "code": 500}})";
            if (is_stream_)
            {
                if (!connection_broken && isConnectionAlive())
                {
                    ResponseTools::post_stream_data(*sink, "error", err, false);
                    ResponseTools::post_stream_data(*sink, "data", "[DONE]", true);
                }
            }
            else
            {
                res->status = 500;
                res->set_content(err, MIMETYPE_JSON);
            }
            My_Log{} << "--- Query Context Failed ---\n" << std::endl;
            return false;
        }
        My_Log{}.original(true) << "\n";
        My_Log{} << "--- Query Context End ---\n" << std::endl;

        auto finalize_generation_result = [&]()
        {

            if (handle->was_stopped_by_output_limit())
            {
                finishReason = "length";
                My_Log{My_Log::Level::kWarning}
                    << "[SendResponse] Generation stopped due to output token limit. "
                    << "finish_reason = \"length\"" << std::endl;
            }

            if (GetEffectivePromptType() == PromptType::Harmony && proc_)
            {
                auto* harmony_proc = dynamic_cast<HarmonyProcessor*>(proc_);
                if (harmony_proc)
                {

                    std::string flushed_final = harmony_proc->FinalizeFinalChannel();
                    if (!flushed_final.empty())
                    {
                        if (ResponseTools::log_inference_stream)
                        {
                            My_Log{My_Log::Level::kInfo}
                                << "[SendResponse] FinalizeFinalChannel flushed " << flushed_final.length()
                                << " bytes of pending final content. "
                                << "Preview: \"" << flushed_final.substr(0, std::min(flushed_final.length(), size_t(80))) << "\""
                                << std::endl;
                        }
                        if (is_stream_ && sink)
                        {
                            ResponseTools::post_stream_data(*sink, "data",
                                ResponseTools::responseDataJson(flushed_final, "", true));
                            if (ResponseTools::log_inference_stream)
                            {
                                My_Log{My_Log::Level::kInfo}
                                    << "[SendResponse] Flushed final content sent to client via SSE." << std::endl;
                            }
                        }
                    }
                    else
                    {
                        if (ResponseTools::log_inference_stream)
                        {
                            My_Log{My_Log::Level::kInfo}
                                << "[SendResponse] FinalizeFinalChannel: no pending final content to flush "
                                << "(normal case when <|return|> was properly received)." << std::endl;
                        }
                    }

                    harmony_proc->FinalizeToolCall();

                    if (harmony_proc->IsToolCall())
                    {
                        isToolResponse = true;
                        toolResponse = harmony_proc->GetToolCallContent();

                    }
                }
            }

            else if (!isToolResponse && is_tool_ && GetEffectivePromptType() == PromptType::General)
            {
                std::string visible_text = extractFinalAnswer(response_buffer);
                std::string wrapped;
                if (ResponseTools::DetectBareToolCall(visible_text, wrapped))
                {
                    isToolResponse = true;
                    toolResponse = wrapped;
                    My_Log{My_Log::Level::kWarning}
                        << "[SendResponse] General format: detected bare JSON tool call (no <tool_call> tag). "
                        << "Preview: " << wrapped.substr(0, std::min(wrapped.size(), size_t(120))) << std::endl;

                }
            }
        };

        finalize_generation_result();
        if (mimicry_degraded && !isToolResponse)
        {
            finishReason = "length";
        }

        if (isToolResponse && instance_config_ && !retry_request_data_.is_null()
            && retry_request_data_.contains("messages") && retry_request_data_["messages"].is_array())
        {

            std::string repetition_gate_key;
            {
                const auto &guard_cfg = model_config_.GetToolCallRepairConfig().redundant_call_guard;
                if (guard_cfg.enabled)
                {
                    const std::string session_key =
                        TaskMemoBuilder::ComputeFirstMsgKey(retry_request_data_["messages"]);
                    repetition_gate_key =
                        ToolCallRepetitionStore::MakeKey(session_key, instance_config_->get_model_name());
                }
                if (!repetition_gate_key.empty())
                {
                    size_t orphan_results = 0;
                    const auto executed =
                        RepeatGateLogic::CollectExecutedOutcomes(retry_request_data_["messages"], &orphan_results);

                    std::vector<std::string> hydrate_order;
                    std::vector<std::vector<ToolCallRepetitionStore::HydratedOutcome>> hydrate_groups;
                    for (const auto &item : executed)
                    {
                        size_t slot = hydrate_order.size();
                        for (size_t i = 0; i < hydrate_order.size(); ++i)
                        {
                            if (hydrate_order[i] == item.signature)
                            {
                                slot = i;
                                break;
                            }
                        }
                        if (slot == hydrate_order.size())
                        {
                            hydrate_order.push_back(item.signature);
                            hydrate_groups.emplace_back();
                        }
                        ToolCallRepetitionStore::HydratedOutcome observation;
                        observation.digest = ToolCallRepetitionStore::ResultDigest(item.content);
                        observation.cls = RepeatGateLogic::ClassifyOutcome(item.content);
                        observation.preview = RepeatGateLogic::MakeOutcomePreview(item.content);
                        hydrate_groups[slot].push_back(std::move(observation));
                    }
                    for (size_t i = 0; i < hydrate_order.size(); ++i)
                    {
                        ToolCallRepetitionStore::GetInstance().HydrateOutcomes(
                            repetition_gate_key, hydrate_order[i], hydrate_groups[i]);
                    }
                    My_Log{My_Log::Level::kDebug}
                        << "[ToolCallRepair][Layer2b] Ledger hydrated: " << executed.size()
                        << " executed call(s) across " << hydrate_order.size() << " signature(s), "
                        << orphan_results << " orphan tool result(s) without a matching call." << std::endl;
                }
            }

            const auto &repair_cfg = model_config_.GetToolCallRepairConfig();
            if (repair_cfg.enabled && repair_cfg.internal_retry.max_attempts > 0)
            {
                ToolCallFailureReason preview_reason = ToolCallFailureReason::kNone;
                const std::string preview_converted = ResponseTools::convertToolCallJson(toolResponse, &preview_reason);

                const bool malformed_needs_retry =
                    IsUnknowToolCallJson(preview_converted)
                    && !IsFailureReasonSkipped(preview_reason, repair_cfg.internal_retry.skip_reasons);

                if (malformed_needs_retry && RepeatGateLogic::TryConsumeExtraRetry(extra_retry_used))
                {
                    My_Log{My_Log::Level::kWarning}
                        << "[ToolCallRepair][Layer2] Malformed tool call detected (reason="
                        << ResponseTools::ToolCallFailureReasonToString(preview_reason)
                        << "). Attempting 1 internal invisible retry (server-side, not visible to client)." << std::endl;

                    const std::string snapshot_response_buffer = response_buffer;
                    const std::string snapshot_tool_response = toolResponse;
                    const std::string snapshot_finish_reason = finishReason;

                    bool retry_yielded_valid_tool_call = false;
                    try
                    {
                        json scratch_data = retry_request_data_;
                        json assistant_msg;
                        assistant_msg["role"] = "assistant";
                        assistant_msg["content"] = response_buffer;
                        json tool_error_msg;
                        tool_error_msg["role"] = "tool";
                        tool_error_msg["content"] = BuildToolRepairErrorMessage(preview_reason);
                        scratch_data["messages"].push_back(assistant_msg);
                        scratch_data["messages"].push_back(tool_error_msg);

                        auto *mutable_instance_config = const_cast<ModelInstanceConfig *>(instance_config_);
                        ChatHistory scratch_chat_history(*mutable_instance_config);
                        ModelInputBuilder scratch_builder(scratch_chat_history, mutable_instance_config, scratch_data);

                        bool scratch_is_tool = false;
                        std::function<bool()> scratch_is_alive_fn = nullptr;
                        if (is_stream_ && sink)
                        {
                            scratch_is_alive_fn = [sink]() -> bool { return sink->is_writable(); };
                        }
                        ModelInput scratch_model_input =
                            scratch_builder.Build(scratch_data, scratch_is_tool, scratch_is_alive_fn);

                        capture_harmony_history_snapshot();

                        response_buffer.clear();
                        toolResponse.clear();
                        isToolResponse = false;
                        degenerate_mimicry_stop = false;
                        mimicry_tail_tokens = 0;
                        proc_->Clean();
                        is_tool_ = scratch_is_tool;

                        bare_json_state = ResponseTools::BareJsonHoldState();
                        harmony_thinking_status_sent = false;
                        last_hold_keepalive_at = std::chrono::steady_clock::now();

                        My_Log{} << "--- Query Context Start (Layer2 internal retry) ---" << std::endl;
                        bool retry_query_ok = handle->Query(scratch_model_input, genie_callback, prefill_heartbeat);
                        My_Log{}.original(true) << "\n";
                        My_Log{} << "--- Query Context End (Layer2 internal retry) ---\n" << std::endl;

                        if (retry_query_ok && !connection_broken)
                        {
                            finalize_generation_result();

                            if (isToolResponse)
                            {
                                ToolCallFailureReason retry_reason = ToolCallFailureReason::kNone;
                                const std::string retry_converted =
                                    ResponseTools::convertToolCallJson(toolResponse, &retry_reason);
                                retry_yielded_valid_tool_call = !IsUnknowToolCallJson(retry_converted);
                                if (!retry_yielded_valid_tool_call)
                                {
                                    My_Log{My_Log::Level::kWarning}
                                        << "[ToolCallRepair][Layer2] Internal retry still malformed (reason="
                                        << ResponseTools::ToolCallFailureReasonToString(retry_reason)
                                        << "). Model likely did not understand the correction hint." << std::endl;
                                }
                            }
                        }
                        else
                        {
                            My_Log{My_Log::Level::kWarning}
                                << "[ToolCallRepair][Layer2] Internal retry Query() failed or connection broke."
                                << std::endl;
                        }
                    }
                    catch (const std::exception &e)
                    {
                        My_Log{My_Log::Level::kError}
                            << "[ToolCallRepair][Layer2] Exception during internal retry: " << e.what() << std::endl;
                        retry_yielded_valid_tool_call = false;
                    }

                    if (retry_yielded_valid_tool_call)
                    {
                        My_Log{} << "[ToolCallRepair][Layer2] Internal retry succeeded, "
                                    "replacing malformed first attempt with corrected result (invisible to client)."
                                 << std::endl;
                    }
                    else
                    {

                        response_buffer = snapshot_response_buffer;
                        toolResponse = snapshot_tool_response;
                        isToolResponse = true;
                        finishReason = snapshot_finish_reason;

                        layer2_retry_rolled_back = true;
                        My_Log{My_Log::Level::kWarning}
                            << "[ToolCallRepair][Layer2] Retry did not help; falling back to existing unknow "
                               "forwarding (unchanged behavior)." << std::endl;
                    }
                }
                else if (!malformed_needs_retry)
                {

                    const auto &rg_cfg = repair_cfg.redundant_call_guard;
                    std::string store_signature;
                    if (rg_cfg.enabled && rg_cfg.soft_retry_enabled && rg_cfg.internal_retry_threshold > 0
                        && !repetition_gate_key.empty())
                    {
                        try
                        {
                            json parsed = json::parse(ResponseTools::extractJsonFromToolCall(preview_converted));
                            const std::string tn = parsed.value("name", std::string());
                            if (!tn.empty())
                                store_signature = tn + "|" + parsed.value("arguments", json::object()).dump();
                        }
                        catch (...) {}
                    }

                    std::string ledger_preview;
                    int ledger_executed = 0;
                    bool gate_wants_soft_retry = false;
                    if (!store_signature.empty())
                    {
                        auto &store = ToolCallRepetitionStore::GetInstance();
                        bool digest_stable = false;
                        store.GetOutcomeSummary(repetition_gate_key, store_signature,
                                                ledger_preview, ledger_executed, digest_stable);
                        gate_wants_soft_retry =
                            ToolCallRepetitionStore::DecideRepeatGate(
                                store.PeekTotalCount(repetition_gate_key, store_signature),
                                static_cast<size_t>(store.PeekDistinctDigestCount(repetition_gate_key, store_signature)),
                                extra_retry_used,
                                rg_cfg.internal_retry_threshold,
                                rg_cfg.hard_block_threshold)
                            == ToolCallRepetitionStore::RepeatGate::kSoftRetry;
                    }

                    if (gate_wants_soft_retry && !ledger_preview.empty()
                        && RepeatGateLogic::TryConsumeExtraRetry(extra_retry_used))
                    {
                        My_Log{My_Log::Level::kWarning}
                            << "[ToolCallRepair][Layer2b] Non-progressing repeat detected (executed="
                            << ledger_executed << ", result unchanged). Attempting 1 internal invisible "
                               "retry with the ledger's recorded result (server-side, not visible to client)."
                            << std::endl;

                        const std::string snapshot_response_buffer = response_buffer;
                        const std::string snapshot_tool_response = toolResponse;
                        const std::string snapshot_finish_reason = finishReason;

                        try
                        {
                            json scratch_data = retry_request_data_;
                            json assistant_msg;
                            assistant_msg["role"] = "assistant";
                            assistant_msg["content"] = response_buffer;
                            json tool_note_msg;
                            tool_note_msg["role"] = "tool";
                            tool_note_msg["content"] =
                                RepeatGateLogic::BuildSoftRetryNote(ledger_preview, ledger_executed);
                            scratch_data["messages"].push_back(assistant_msg);
                            scratch_data["messages"].push_back(tool_note_msg);

                            auto *mutable_instance_config = const_cast<ModelInstanceConfig *>(instance_config_);
                            ChatHistory scratch_chat_history(*mutable_instance_config);
                            ModelInputBuilder scratch_builder(scratch_chat_history, mutable_instance_config, scratch_data);

                            bool scratch_is_tool = false;
                            std::function<bool()> scratch_is_alive_fn = nullptr;
                            if (is_stream_ && sink)
                                scratch_is_alive_fn = [sink]() -> bool { return sink->is_writable(); };
                            ModelInput scratch_model_input =
                                scratch_builder.Build(scratch_data, scratch_is_tool, scratch_is_alive_fn);

                            capture_harmony_history_snapshot();

                            response_buffer.clear();
                            toolResponse.clear();
                            isToolResponse = false;
                            proc_->Clean();
                            is_tool_ = scratch_is_tool;
                            bare_json_state = ResponseTools::BareJsonHoldState();
                            harmony_thinking_status_sent = false;
                            last_hold_keepalive_at = std::chrono::steady_clock::now();

                            My_Log{} << "--- Query Context Start (Layer2b repetition retry) ---" << std::endl;
                            bool retry_query_ok = handle->Query(scratch_model_input, genie_callback, prefill_heartbeat);
                            My_Log{}.original(true) << "\n";
                            My_Log{} << "--- Query Context End (Layer2b repetition retry) ---\n" << std::endl;

                            if (retry_query_ok && !connection_broken)
                            {
                                finalize_generation_result();
                                My_Log{} << "[ToolCallRepair][Layer2b] Repetition retry completed; "
                                             "replacing repeated call with model's reconsidered response "
                                             "(invisible to client)." << std::endl;
                            }
                            else
                            {
                                response_buffer = snapshot_response_buffer;
                                toolResponse = snapshot_tool_response;
                                isToolResponse = true;
                                finishReason = snapshot_finish_reason;
                                layer2_retry_rolled_back = true;
                                My_Log{My_Log::Level::kWarning}
                                    << "[ToolCallRepair][Layer2b] Repetition retry Query() failed or "
                                       "connection broke; falling back to original repeated call."
                                    << std::endl;
                            }
                        }
                        catch (const std::exception &e)
                        {
                            response_buffer = snapshot_response_buffer;
                            toolResponse = snapshot_tool_response;
                            isToolResponse = true;
                            finishReason = snapshot_finish_reason;
                            layer2_retry_rolled_back = true;
                            My_Log{My_Log::Level::kError}
                                << "[ToolCallRepair][Layer2b] Exception during repetition retry: "
                                << e.what() << std::endl;
                        }
                    }
                }
            }
        }

        auto finalize_tool_call_layer3 = [&](const char *log_suffix)
        {
            ToolCallFailureReason failure_reason = ToolCallFailureReason::kNone;
            json partial_recovery;
            toolResponse = ResponseTools::convertToolCallJson(toolResponse, &failure_reason, &partial_recovery);

            {
                const auto skill_mappings = skill_mappings_set_ ? skill_mappings_ : model_config_.GetRuntimeSkillMappings();
                const auto& opt_cfg = model_config_.GetPromptOptimizationConfig();
                toolResponse = ResponseTools::AutoCorrectSkillCall(
                    toolResponse, skill_mappings, opt_cfg.enable_skill_auto_correction);
            }
            My_Log{} << "ResponseDispatcher::SendResponse" << log_suffix << ": \n" << toolResponse << std::endl;

            auto send_tool_call_status_once = [&](const std::string &tool_name)
            {
                if (!status_tool_call_sent_)
                {
                    status_tool_call_sent_ = true;
                    if (is_stream_ && sink)
                    {
                        auto name_status = GetToolCallStatusByName(tool_name);
                        if (!name_status.first.empty())
                        {
                            SendStatusUpdate(sink, name_status.first, name_status.second);
                        }
                        else
                        {
                            SendStatusUpdate(sink, "tool_call", "Calling tool...");
                        }
                    }
                }
            };

            std::string circuit_breaker_key;
            std::string repetition_key;
            std::string think_session_key;
            if (instance_config_ && !retry_request_data_.is_null()
                && retry_request_data_.contains("messages") && retry_request_data_["messages"].is_array())
            {
                const std::string session_key = TaskMemoBuilder::ComputeFirstMsgKey(retry_request_data_["messages"]);
                think_session_key = session_key;
                circuit_breaker_key = ToolCallCircuitBreakerStore::MakeKey(session_key, instance_config_->get_model_name());
                repetition_key = ToolCallRepetitionStore::MakeKey(session_key, instance_config_->get_model_name());
            }
            const bool redundant_guard_enabled = model_config_.GetToolCallRepairConfig().redundant_call_guard.enabled;
            const bool output_truncated = handle->was_stopped_by_output_limit();

            if (!IsUnknowToolCallJson(toolResponse))
            {

                ToolCallCircuitBreakerStore::GetInstance().RecordSuccess(circuit_breaker_key);

                std::string gate_tool_name;
                std::string gate_signature;
                try
                {
                    json gate_parsed = json::parse(ResponseTools::extractJsonFromToolCall(toolResponse));
                    if (gate_parsed.contains("name") && gate_parsed["name"].is_string())
                        gate_tool_name = gate_parsed["name"].get<std::string>();
                    if (!gate_tool_name.empty())
                        gate_signature = gate_tool_name + "|" + gate_parsed.value("arguments", json::object()).dump();
                }
                catch (...) {}

                bool hard_blocked = false;
                if (redundant_guard_enabled && !gate_signature.empty() && !repetition_key.empty())
                {
                    const auto &gate_cfg = model_config_.GetToolCallRepairConfig().redundant_call_guard;
                    auto &gate_store = ToolCallRepetitionStore::GetInstance();
                    const int gate_total = gate_store.PeekTotalCount(repetition_key, gate_signature);
                    const size_t gate_distinct =
                        static_cast<size_t>(gate_store.PeekDistinctDigestCount(repetition_key, gate_signature));

                    const auto gate_decision = ToolCallRepetitionStore::DecideRepeatGate(
                        gate_total, gate_distinct, extra_retry_used, 0, gate_cfg.hard_block_threshold);
                    if (gate_decision == ToolCallRepetitionStore::RepeatGate::kHardBlock)
                    {
                        std::string gate_preview;
                        int gate_executed = 0;
                        bool gate_digest_stable = false;
                        gate_store.GetOutcomeSummary(repetition_key, gate_signature, gate_preview,
                                                     gate_executed, gate_digest_stable);

                        const int gate_blocked = gate_store.RecordBlocked(repetition_key, gate_signature);
                        const bool gate_final = gate_cfg.hard_block_stop_after > 0
                            && gate_blocked >= gate_cfg.hard_block_stop_after;
                        repeat_block_body = gate_final
                            ? RepeatGateLogic::BuildHardBlockFinalBody(gate_tool_name, gate_executed,
                                                                      gate_blocked, gate_preview)
                            : RepeatGateLogic::BuildHardBlockBody(gate_tool_name, gate_executed, gate_preview);
                        finishReason = gate_final ? "stop" : "length";
                        isToolResponse = false;
                        toolResponse.clear();
                        hard_blocked = true;
                        My_Log{My_Log::Level::kWarning}
                            << "[ToolCallRepair][Layer2b] HARD BLOCK: '" << gate_tool_name
                            << "' already executed " << gate_executed
                            << " time(s) with an unchanged result (total=" << gate_total
                            << ", distinct_digests=" << gate_distinct
                            << ", blocked_count=" << gate_blocked
                            << "); the call is NOT forwarded to the client. Downgrading to plain text with "
                               "finish_reason=\"" << finishReason << "\"" << (gate_final ? " (final stop)" : "")
                            << ". Generated tail: " << TailBytes(response_buffer, 200) << std::endl;
                    }
                }

                if (!hard_blocked)
                {
                    finishReason = output_truncated ? "length" : "tool_calls";
                    std::string tool_name;
                    try
                    {
                        json parsed = json::parse(ResponseTools::extractJsonFromToolCall(toolResponse));
                        if (parsed.contains("name") && parsed["name"].is_string())
                        {
                            tool_name = parsed["name"].get<std::string>();
                        }
                        if (!tool_name.empty())
                        {
                            const std::string signature = tool_name + "|" + parsed.value("arguments", json::object()).dump();
                            if (redundant_guard_enabled)
                                ToolCallRepetitionStore::GetInstance().RecordCall(repetition_key, signature);
                            const bool reasoning_note_mimicry = TaskMemoBuilder::IsReasoningNoteMimicry(response_buffer);
                            std::string conclusion = reasoning_note_mimicry
                                ? std::string()
                                : TaskMemoBuilder::ExtractThinkConclusion(response_buffer);
                            if (output_truncated && !reasoning_note_mimicry)
                                conclusion += (conclusion.empty() ? "" : " ") + std::string("this call was cut off by the output token limit after ")
                                    + std::to_string(response_buffer.size()) + " characters, so its arguments are incomplete; split the content into several smaller calls";
                            if (!conclusion.empty())
                                TaskMemoBuilder::PinThinkConclusion(think_session_key, signature, conclusion);
                        }
                    }
                    catch (...) {}
                    send_tool_call_status_once(tool_name);
                }
            }
            else
            {
                int consecutive = ToolCallCircuitBreakerStore::GetInstance().RecordLayer3Trigger(circuit_breaker_key);
                My_Log{My_Log::Level::kWarning}
                    << "[ToolCallRepair][Layer3] Layer0/1/2 all failed to recover a valid tool call"
                    << log_suffix << " (reason=" << ResponseTools::ToolCallFailureReasonToString(failure_reason)
                    << ", consecutive_layer3_triggers=" << consecutive << "). Selecting terminal protocol..." << std::endl;

                if (partial_recovery.contains("name") && partial_recovery["name"].is_string()
                    && !partial_recovery["name"].get<std::string>().empty())
                {

                    json best_effort = json::object();
                    best_effort["name"] = partial_recovery["name"];
                    best_effort["arguments"] = partial_recovery.value("arguments", json::object());
                    toolResponse = ResponseTools::wrapJsonInToolCall(best_effort.dump());
                    finishReason = output_truncated ? "length" : "tool_calls";
                    isToolResponse = true;
                    send_tool_call_status_once(partial_recovery["name"].get<std::string>());
                    if (redundant_guard_enabled)
                    {
                        const std::string signature = partial_recovery["name"].get<std::string>() + "|"
                            + best_effort["arguments"].dump();
                        ToolCallRepetitionStore::GetInstance().RecordCall(repetition_key, signature);
                    }
                    My_Log{My_Log::Level::kWarning}
                        << "[ToolCallRepair][Layer3] Case A: emitting real tool_call '"
                        << partial_recovery["name"].get<std::string>()
                        << "' with best-effort (possibly incomplete) arguments." << std::endl;
                }
                else
                {

                    finishReason = "length";
                    isToolResponse = false;
                    toolResponse.clear();
                    My_Log{My_Log::Level::kWarning}
                        << "[ToolCallRepair][Layer3] Case B: no recoverable tool name; downgrading to plain "
                           "text with finish_reason=\"length\" (never \"stop\")." << std::endl;
                }
            }
        };

        bool had_tool_call_attempt = isToolResponse;
        if (had_tool_call_attempt)
        {
            finalize_tool_call_layer3(connection_broken ? " (connection_broken)" : "");
        }
        last_finish_reason_ = finishReason;
        last_tool_response_ = isToolResponse ? toolResponse : std::string();

        if (!had_tool_call_attempt && !bare_json_state.held_visible.empty() && is_stream_ && sink)
        {
            ResponseTools::post_stream_data(*sink, "data",
                ResponseTools::responseDataJson(bare_json_state.held_visible, "", true));
            bare_json_state.held_visible.clear();
        }

        if (!GetEffectiveStatelessMode())
        {

            if (GetEffectivePromptType() == PromptType::Harmony && proc_)
            {

                auto* harmony_proc = dynamic_cast<HarmonyProcessor*>(proc_);
                if (harmony_proc)
                {

                    std::string history_msg = (layer2_retry_rolled_back && has_harmony_history_snapshot)
                                                   ? snapshot_harmony_history_msg
                                                   : harmony_proc->GetMessageForHistory();

                    if (!repeat_block_body.empty())
                    {
                        history_msg = repeat_block_body;
                    }

                    if (layer2_retry_rolled_back && has_harmony_history_snapshot)
                    {
                        const std::string would_be_corrupted = harmony_proc->GetMessageForHistory();
                        My_Log{My_Log::Level::kWarning}
                            << "[ToolCallRepair][Layer2][HistorySnapshot] rollback occurred; using pre-retry "
                               "snapshot for ChatHistory (len=" << history_msg.length()
                            << ") instead of live post-retry proc_ state (len=" << would_be_corrupted.length()
                            << "). Divergence=" << (would_be_corrupted != history_msg ? "YES" : "no (identical)")
                            << std::endl;
                    }

                    if (!history_msg.empty())
                    {
                        chatHistory.AddMessage("assistant", history_msg);

                        const bool is_tool_call_for_log = (layer2_retry_rolled_back && has_harmony_history_snapshot)
                                                               ? snapshot_harmony_is_tool_call
                                                               : harmony_proc->IsToolCall();
                        if (is_tool_call_for_log)
                        {
                            My_Log{My_Log::Level::kDebug} << "[History] ✓ Added tool-related assistant message (full CoT preserved)" << std::endl;
                            My_Log{My_Log::Level::kDebug} << "[History]   - Includes: analysis + commentary + final channels" << std::endl;
                        }
                        else
                        {
                            My_Log{My_Log::Level::kDebug} << "[History] ✓ Added regular assistant message (final channel only)" << std::endl;
                            My_Log{My_Log::Level::kDebug} << "[History]   - CoT (analysis) discarded per Harmony spec" << std::endl;
                        }

                        My_Log{My_Log::Level::kDebug} << "[History]   - Message length: " << history_msg.length() << " bytes" << std::endl;
                    }
                    else
                    {
                        My_Log{My_Log::Level::kInfo}
                            << "[History] ⚠ Empty history message from Harmony processor" << std::endl;
                    }
                }
                else
                {
                    My_Log{My_Log::Level::kError}
                        << "[History] ✗ Failed to cast processor to HarmonyProcessor" << std::endl;

                    chatHistory.AddMessage("assistant", extractFinalAnswer(response_buffer));
                }
            }
            else
            {

                std::string final_answer = !repeat_block_body.empty()
                                                ? repeat_block_body
                                                : ((had_tool_call_attempt && !isToolResponse)
                                                        ? extractSanitizedFinalText(response_buffer)
                                                        : extractFinalAnswer(response_buffer));
                chatHistory.AddMessage("assistant", final_answer);

                My_Log{My_Log::Level::kDebug} << "[History] ✓ Added assistant message (standard format)" << std::endl;
            }
        }
        else
        {
            My_Log{My_Log::Level::kDebug} << "[History] Skipped (numResponse == -1, client manages history)" << std::endl;
        }

        PrintProfile();

        json profile_for_usage = handle->HandleProfile();
        size_t real_prompt_tokens = (profile_for_usage.is_object()
                                      && profile_for_usage.contains("num_prompt_tokens")
                                      && profile_for_usage["num_prompt_tokens"].is_number())
            ? profile_for_usage["num_prompt_tokens"].get<size_t>() : 0;
        size_t real_completion_tokens = (profile_for_usage.is_object()
                                          && profile_for_usage.contains("num_generated_tokens")
                                          && profile_for_usage["num_generated_tokens"].is_number())
            ? profile_for_usage["num_generated_tokens"].get<size_t>() : 0;

        bool overflow_truncated = suppress_end_on_overflow && handle->was_stopped_by_output_limit();
        if (connection_broken)
        {
            My_Log{My_Log::Level::kWarning}
                << "[SendResponse] Connection was broken during generation. "
                << "History and profile stored. Attempting to send end-of-stream markers anyway." << std::endl;

            if (is_stream_ && sink)
            {

                if (had_tool_call_attempt)
                {
                    if (isToolResponse)
                    {

                        std::string content;
                        if (!GetEffectiveIsOutputAllText())
                        {
                            content = ResponseTools::remove_tool_call_content(toolResponse);
                        }
                        if (!content.empty())
                        {
                            content += "\n\n";
                        }
                        std::string response_data = ResponseTools::responseDataJson(content, "", true, toolResponse);
                        My_Log{} << "[Tool Call Response] Sending to client (connection_broken): " << response_data << std::endl;
                        ResponseTools::post_stream_data(*sink, "data", response_data);
                    }
                    else
                    {

                        std::string content = !repeat_block_body.empty()
                                                  ? repeat_block_body
                                                  : extractSanitizedFinalText(response_buffer);
                        if (!content.empty())
                        {
                            ResponseTools::post_stream_data(*sink, "data",
                                ResponseTools::responseDataJson(content, "", true));
                        }
                    }
                }

                if (!overflow_truncated)
                {
                    ResponseTools::post_stream_data(*sink, "data",
                        ResponseTools::responseDataJson("", finishReason, true, "", real_prompt_tokens, real_completion_tokens));
                    ResponseTools::post_stream_data(*sink, "data", "[DONE]", true);
                    My_Log{My_Log::Level::kWarning}
                        << "[SendResponse] End-of-stream markers sent (connection_broken path)." << std::endl;
                }
                else
                {
                    My_Log{My_Log::Level::kWarning}
                        << "[SendResponse] suppress_end_on_overflow=true, skipping [DONE] (connection_broken path). "
                        << "Caller will handle cloud fallback or send end markers." << std::endl;
                }
            }
            return false;
        }

        if (had_tool_call_attempt)
        {
            if (isToolResponse)
            {

                std::string content;

                if (!GetEffectiveIsOutputAllText())
                {
                    content = ResponseTools::remove_tool_call_content(toolResponse);
                }
                if (!content.empty())
                {
                    content += "\n\n";
                }

                if (is_stream_)
                {
                    std::string response_data = ResponseTools::responseDataJson(content, "", true, toolResponse);
                    My_Log{} << "[Tool Call Response] Sending to client: " << response_data << std::endl;
                    ResponseTools::post_stream_data(*sink, "data", response_data);
                }
            }
            else if (is_stream_ && sink)
            {

                std::string content = !repeat_block_body.empty()
                                          ? repeat_block_body
                                          : extractSanitizedFinalText(response_buffer);
                if (!content.empty())
                {
                    ResponseTools::post_stream_data(*sink, "data",
                        ResponseTools::responseDataJson(content, "", true));
                }
            }
        }

        if (is_stream_)
        {
            if (!overflow_truncated)
            {
                ResponseTools::post_stream_data(*sink, "data",
                    ResponseTools::responseDataJson("", finishReason, true, "", real_prompt_tokens, real_completion_tokens));
                ResponseTools::post_stream_data(*sink, "data", "[DONE]", true);
            }
            else
            {
                My_Log{My_Log::Level::kWarning}
                    << "[SendResponse] suppress_end_on_overflow=true, skipping [DONE]. "
                    << "Caller will handle cloud fallback or send end markers." << std::endl;
            }
        }
        else
        {

            std::string content = !repeat_block_body.empty()
                                      ? repeat_block_body
                                      : extractSanitizedFinalText(response_buffer);
            if (WatermarkProviderHost::Instance().HasTextHook())
            {
                const auto* vt = WatermarkProviderHost::Instance().GetVTable();
                char* watermarked = vt->text_hook_apply(content.c_str());
                if (watermarked)
                {
                    content = watermarked;
                    vt->text_hook_free_string(watermarked);
                }
            }
            auto data = ResponseTools::responseDataJson(content, finishReason, false, toolResponse,
                                                         real_prompt_tokens, real_completion_tokens,
                                                         proc_->GetReasoningContent());
            res->set_content(data, MIMETYPE_JSON);
        }
        return true;
    }
    catch (const std::exception &e)
    {
        My_Log{My_Log::Level::kError} << "raise the exception while processing stream response: \n"
                                      << e.what() << "\n";

        if (!is_stream_) {
            res->status = 500;
            res->set_content(R"({"error": {"message": "Internal server error", "type": "server_error", "code": 500}})", MIMETYPE_JSON);
            return false;
        }

        if (!req_->is_connection_closed())
        {
            json error_json;
            if (dynamic_cast<const ReportError *>(&e))
            {
                error_json = {{"error", {{"message", e.what()}, {"type", "report_error"}, {"code", 400}}}};
            }
            else
            {
                error_json = {{"error", {{"message", std::string("Model generation error: ") + e.what()}, {"type", "server_error"}, {"code", 500}}}};
            }
            ResponseTools::post_stream_data(*sink, "error", error_json.dump(), false);
            ResponseTools::post_stream_data(*sink, "data", "[DONE]", true);
        }
        return false;
    }
}

bool ResponseDispatcher::isConnectionAlive() const
{
    if (stream_write_failed_.load())
        return false;
    if (!req_)
        return true;

    auto closed = req_->is_connection_closed();
    if (closed)
    {
        My_Log{My_Log::Level::kError} << "Client connection has been broken (Client or Proxy disconnected proactively)\n" << std::endl;
    }
    return !closed;
}

ResponseDispatcher::~ResponseDispatcher()
{
    if (proc_)
    {
        delete proc_;
        proc_ = nullptr;
    }
}

std::string ResponseDispatcher::extractFinalAnswer(const std::string &output)
{

    if (GetEffectivePromptType() == PromptType::Harmony)
    {

        if (proc_)
        {
            return dynamic_cast<HarmonyProcessor*>(proc_)->GetFinalContent();
        }
        return output;
    }
    else
    {

        const std::string tag = "</think>";
        size_t pos = output.find(tag);
        if (pos != std::string::npos)
        {

            return output.substr(pos + tag.length());
        }
        else
        {

            return output;
        }
    }
}

std::string ResponseDispatcher::extractSanitizedFinalText(const std::string &output)
{

    return ResponseTools::remove_tool_call_content(extractFinalAnswer(output));
}
