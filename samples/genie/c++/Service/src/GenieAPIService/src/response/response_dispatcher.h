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

#include <nlohmann/json.hpp>

using json = nlohmann::ordered_json;

#include "../processor/processor.h"
#include "../model/def.h"

class ChatHistory;

class ModelInput;

class IModelConfig;

class ModelProcessor;

class ResponseDispatcher
{
public:
    ResponseDispatcher(IModelConfig &model_mgr,
                       ChatHistory &chatHistory);

    ~ResponseDispatcher();

    void ResetProcessor();

    void Prepare(ModelInput &model_input,
                 bool is_tool,
                 bool is_stream,
                 const httplib::Request &req);

    bool SendResponse(size_t, httplib::DataSink *sink, httplib::Response *res);

    static inline std::string MIMETYPE_JSON = "application/json; charset=utf-8";

private:
    void PrintProfile();

    bool isConnectionAlive() const;

    static std::string extractFinalAnswer(const std::string &output)
    {
        const std::string tag = "</think>";
        size_t pos = output.find(tag);
        if (pos != std::string::npos)
        {
            // Extract the content after the </think>.
            return output.substr(pos + tag.length());
        }
        else
        {
            // If the <think> tag is not in the result, return the original string.
            return output;
        }
    }

    std::tuple<bool, std::string> preprocessStream(const std::string &chunkText,
                                                   bool isToolResponse,
                                                   std::string &toolResponse)
    {
        return proc_->preprocessStream(chunkText, isToolResponse, toolResponse);
    }

    bool is_stream_{};
    bool is_tool_{};
    ChatHistory &chatHistory;
    IModelConfig &model_config_;
    std::string response_buffer; // The response buffer is used to store the response content of the model.
    httplib::Request *req_{};
    ModelProcessor *proc_{};
    ModelInput model_input_;
};

#endif //RESPONSE_DISPATCHER_H
