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

#include "log.h"
#include "../processor/general.h"
#include "../processor/harmony.h"

ResponseDispatcher::ResponseDispatcher(IModelConfig &model_mgr,
                                       ChatHistory &chatHistory) :
        chatHistory(chatHistory),
        model_config_(model_mgr)
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

    switch (int(model_config_.get_prompt_type()))
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
                                 const httplib::Request &req)
{
    this->req_ = &const_cast<httplib::Request &>(req);
    this->model_input_ = model_input;
    is_stream_ = is_stream;
    is_tool_ = is_tool;
    proc_->Clean();
}

bool ResponseDispatcher::SendResponse(size_t, httplib::DataSink *sink, httplib::Response *res)
{

    auto handle = model_config_.get_genie_model_handle().lock();
    std::string toolResponse; // Save tool call information
    std::string finishReason = "stop";
    response_buffer.clear();
    bool isToolResponse = false;


    auto genie_callback = [&](std::string &chunk)
    {
        My_Log{}.original(true) << chunk;
        if (!isConnectionAlive())
        {
            handle->Stop();
            return false;
        }

        auto result = preprocessStream(chunk, isToolResponse, toolResponse);
        isToolResponse = std::get<0>(result);
        std::string keepChunk = std::get<1>(result);

        response_buffer += chunk;
        // TODO: If tool call and not output all text, return.
        if (is_tool_ && isToolResponse && !model_config_.getisOutputAllText())
            return true;

        if (is_stream_)
            ResponseTools::post_stream_data(*sink, "data", ResponseTools::responseDataJson(chunk, "", true));
        return true;
    };

    try
    {
        My_Log{} << "~~~~~~~~~~~~~~Query Context Start~~~~~~~~~~~~~~~~~~" << std::endl;
        if (!handle->Query(model_input_, genie_callback))
        {
            constexpr char *err = R"({"error": "Model query unavailable"})";
            if (is_stream_)
                ResponseTools::post_stream_data(*sink, "data", err, true);
            else
                res->set_content(err, MIMETYPE_JSON);
            My_Log{} << "~~~~~~~~~~~~~~~Query Context Failed~~~~~~~~~~~~~~~~~~~\n" << std::endl;
            return false;
        }
        My_Log{}.original(true) << "\n";
        My_Log{} << "~~~~~~~~~~~~~~~Query Context End~~~~~~~~~~~~~~~~~~~\n" << std::endl;

        // If there is a tool call, return the processed characters to the client.
        if (isToolResponse)
        {
            toolResponse = ResponseTools::convertToolCallJson(toolResponse);
            My_Log{} << "Extracted JSON 2: " << toolResponse << std::endl;

            finishReason = "tool_calls";
            std::string content;

            if (!model_config_.getisOutputAllText())
            {
                content = ResponseTools::remove_tool_call_content(toolResponse);
            }
            if (!content.empty())
            {
                content += "\n\n";
            }

            if (is_stream_)
                ResponseTools::post_stream_data(*sink, "data",
                                                ResponseTools::responseDataJson(content, "", true, toolResponse));
        }

        chatHistory.AddMessage("assistant", extractFinalAnswer(response_buffer));
        PrintProfile();

        // Send end reason
        if (is_stream_)
        {
            ResponseTools::post_stream_data(*sink, "data", ResponseTools::responseDataJson("", finishReason, true));
            ResponseTools::post_stream_data(*sink, "data", "[DONE]", true);
        }
        else
        {
            auto data = ResponseTools::responseDataJson(response_buffer, finishReason, false, toolResponse);
            res->set_content(data, MIMETYPE_JSON);
        }
        return true;
    }
    catch (const std::exception &e)
    {
        if (!is_stream_)
            throw;
        My_Log{My_Log::Level::kError} << "raise the exception while processing stream response: \n"
                                      << e.what() << "\n";
        if (dynamic_cast<const ReportError *>(&e))
        {
            ResponseTools::post_stream_data(*sink, "data", e.what(), true);
        }
        else
        {
            ResponseTools::post_stream_data(*sink, "data", "raise the exception while processing stream response");
        }
        return false;
    }
}

bool ResponseDispatcher::isConnectionAlive() const
{
    auto closed = req_->is_connection_closed();
    if (closed)
    {
        http_busy_ = false;
        My_Log{My_Log::Level::kError} << "connection has been broken\n" << std::endl;
    }
    return !closed;
}

void ResponseDispatcher::PrintProfile()
{
    My_Log{} << "~~~~~~~~~~~~~~Token Summary Start~~~~~~~~~~~~~~~~~~" << std::endl;
    auto json_str = model_config_.get_genie_model_handle().lock()->HandleProfile();
    if (json_str.empty())
    {
        goto done;
    }

    try
    {
        My_Log{} << "Time to First Token: "
                 << std::fixed
                 << std::setprecision(2)
                 << json_str.at("time_to_first_token").get<std::string>()
                 << " ms" << std::endl;

        My_Log{} << "Token Generation Time: "
                 << std::fixed
                 << std::setprecision(2)
                 << json_str.at("token_generation_time").get<std::string>()
                 << " ms" << std::endl;

        My_Log{} << "Num Prompt Tokens: "
                 << json_str.at("num_prompt_tokens")
                 << ", Text Length: " << model_input_.text_.size()
                 << std::endl;

        My_Log{} << "Prompt Processing Rate: "
                 << std::fixed
                 << std::setprecision(2)
                 << json_str.at("prompt_processing_rate").get<std::string>()
                 << " toks/sec" << std::endl;

        My_Log{} << "Num Generated Tokens: "
                 << json_str.at("num_generated_tokens")
                 << ", Text Length: " << response_buffer.size()
                 << std::endl;

        My_Log{} << "Token Generation Rate: "
                 << std::fixed
                 << std::setprecision(2)
                 << json_str.at("token_generation_rate").get<std::string>()
                 << " toks/sec" << std::endl;
    }
    catch (std::exception &e)
    {
        My_Log{My_Log::Level::kError} << "printf profile failed:" << e.what() << std::endl;
    }

    done:
    My_Log{} << "~~~~~~~~~~~~~~~Token Summary End~~~~~~~~~~~~~~~~~~~\n";
}

ResponseDispatcher::~ResponseDispatcher()
{
    if (proc_)
    {
        delete proc_;
        proc_ = nullptr;
    }
}