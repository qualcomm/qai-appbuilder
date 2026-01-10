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

using namespace httplib;

#include <nlohmann/json.hpp>

using json = nlohmann::ordered_json;

#include "../processor/processor.h"

class ChatHistory;

class IModelConfig;

class Prompt;

class ResponseDispatcher
{
public:
    ResponseDispatcher(IModelConfig &model_mgr,
                       ChatHistory &chatHistory);

    ~ResponseDispatcher();

    void ResetProcessor();

    bool Prepare(json &data,
                 const httplib::Request &req);

    bool sendStreamResponse(size_t, httplib::DataSink &sink);

    void sendNormalResponse(httplib::Response &res);

    bool isConnectionAlive() const;

private:
    void PrintProfile();

    std::string extractFinalAnswer(const std::string &output)
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

    std::tuple<bool, std::string> preprocessStream(std::string &chunkText,
                                                   bool isToolResponse,
                                                   std::string &toolResponse)
    { return proc_->preprocessStream(chunkText, isToolResponse, toolResponse); }

    bool is_tool_{};
    Prompt* prompt_;
    ChatHistory &chatHistory;
    IModelConfig &model_config_;
    std::string response_buffer; // The response buffer is used to store the response content of the model.

    std::function<std::string(json &data)> input_parser_{};
    std::string model_input_content_;
    httplib::Request *req_{};
    ModelProcessor *proc_{};
};

#endif //RESPONSE_DISPATCHER_H
