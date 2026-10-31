//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef RESPONSE_DISPATCHER_H
#define RESPONSE_DISPATCHER_H

#include <httplib.h>

#include <atomic>
#include <nlohmann/json.hpp>

using json = nlohmann::ordered_json;

#include "../processor/processor.h"
#include "../processor/harmony.h"
#include "../model/def.h"
#include "../context/context_base.h"
#include "../model/model_config.h"
#include "../model/model_instance_config.h"

class ChatHistory;

class ModelInput;

class ModelProcessor;

class ResponseDispatcher
{
public:

    ResponseDispatcher(IModelConfig &model_mgr,
                       ChatHistory &chatHistory,
                       const ModelInstanceConfig *instance_config = nullptr);

    ~ResponseDispatcher();

    void ResetProcessor();

    void SetSkillMappings(SkillMappings mappings)
    {
        skill_mappings_ = std::move(mappings);
        skill_mappings_set_ = true;
    }

    void Prepare(ModelInput &model_input,
                 bool is_tool,
                 bool is_stream,
                 const httplib::Request &req,
                 bool is_dll_mode = false,
                 const json *request_data_for_retry = nullptr);

    bool SendResponse(size_t, httplib::DataSink *sink, httplib::Response *res, bool suppress_end_on_overflow = false);

    void SendStatusUpdate(httplib::DataSink *sink, const std::string &status, const std::string &message);

    void SendKeepAlive(httplib::DataSink *sink);

    static std::pair<std::string, std::string> GetToolCallStatusByName(const std::string &tool_name);

    static inline std::string MIMETYPE_JSON = "application/json; charset=utf-8";

    const std::string &GetFinishReason() const { return last_finish_reason_; }

    const std::string &GetToolResponse() const { return last_tool_response_; }

    const std::string &GetRawOutput() const { return response_buffer; }

    const std::string &GetPromptText() const { return model_input_.text_; }

private:
    void PrintProfile();

    bool isConnectionAlive() const;

    std::string extractFinalAnswer(const std::string &output);

    std::string extractSanitizedFinalText(const std::string &output);

    PromptType GetEffectivePromptType() const
    {
        return instance_config_ ? instance_config_->get_prompt_type() : model_config_.get_prompt_type();
    }

    int GetEffectiveNumResponse() const
    {
        return instance_config_ ? instance_config_->getnumResponse() : model_config_.getnumResponse();
    }

    bool GetEffectiveStatelessMode() const
    {
        return GetEffectiveNumResponse() == -1;
    }

    bool GetEffectiveIsOutputAllText() const
    {
        return instance_config_ ? instance_config_->getisOutputAllText() : model_config_.getisOutputAllText();
    }

    std::shared_ptr<ContextBase> GetEffectiveHandle()
    {
        return instance_config_ ? const_cast<ModelInstanceConfig*>(instance_config_)->i_model_config_.get_genie_model_handle().lock()
                                 : model_config_.get_genie_model_handle().lock();
    }

    std::tuple<bool, std::string> preprocessStream(std::string &chunkText,
                                                   bool isToolResponse,
                                                   std::string &toolResponse)
    {
        return proc_->preprocessStream(chunkText, isToolResponse, toolResponse);
    }

    bool is_stream_{};
    bool is_tool_{};
    std::atomic<bool> stream_write_failed_{false};
    std::atomic<bool> sink_write_wrapped_{false};

    bool status_tool_call_sent_{false};
    bool status_code_sent_{false};

    ChatHistory &chatHistory;
    IModelConfig &model_config_;

    const ModelInstanceConfig *instance_config_{nullptr};
    std::string response_buffer;
    httplib::Request *req_{};
    ModelProcessor *proc_{};
    ModelInput model_input_;

    json retry_request_data_;
    std::string last_finish_reason_;
    std::string last_tool_response_;
    SkillMappings skill_mappings_;
    bool skill_mappings_set_{false};
};

#endif
