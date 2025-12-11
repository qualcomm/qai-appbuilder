#include "core/response_dispatcher.h"
#include <nlohmann/json.hpp>

#include "response_tools.h"
#include "core/log.h"

#include "core/utils.h"
#include "../../processor/general.h"
#include "../../processor/harmony.h"

using json = nlohmann::ordered_json;

ResponseDispatcher::ResponseDispatcher(ModelManager &model_mgr,
                                       ChatHistory &chatHistory)

        : model_config_(model_mgr),
          chatHistory(chatHistory)
{
    ResetProcessor();
}

void ResponseDispatcher::Prepare(const std::string &modelInputContent,
                                 const Request &req,
                                 bool is_tool)
{
    this->req_ = &const_cast<Request &>(req);
    this->model_input_content_ = modelInputContent;
    this->is_tool_ = is_tool;
    proc_->Clean();
}

bool ResponseDispatcher::sendStreamResponse(size_t, DataSink &sink)
{
    try
    {
        auto handle = model_config_.get_genie_model_handle().lock();
        std::string toolResponse;   // Save tool call information
        std::string finishReason = "stop";
        response_buffer.clear();
        bool isToolResponse = false;

        auto genie_callback = [&](std::string &message)
        {
            My_Log{}.original(true)  << message;
            if (!isConnectionAlive())
            {
                handle->Stop();
                return false;
            }

            std::string chunk = message;
            auto result = preprocessStream(chunk, isToolResponse, toolResponse);
            isToolResponse = std::get<0>(result);
            std::string keepChunk = std::get<1>(result);

            response_buffer += chunk;
            // TODO: If tool call and not output all text, return.
            if (is_tool_ && isToolResponse && !model_config_.getisOutputAllText())
                return true;

            ResponseTools::post_stream_data(sink, "data", ResponseTools::responseDataJson(chunk, "", true));
            return true;
        };

        // Send empty data, compatible with SSE client.
        ResponseTools::post_stream_data(sink, "data", ResponseTools::responseDataJson("", "", true));
        My_Log{} << "~~~~~~~~~~~~~~Query Context Start~~~~~~~~~~~~~~~~~~" << std::endl;
        handle->Query(model_input_content_, genie_callback);
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

            ResponseTools::post_stream_data(sink, "data",
                                            ResponseTools::responseDataJson(content, "", true, toolResponse));
        }

        chatHistory.AddMessage("assistant", extractFinalAnswer(response_buffer));

        // Send end reason
        ResponseTools::post_stream_data(sink, "data", ResponseTools::responseDataJson("", finishReason, true));
        std::string done = "data: [DONE]\n\n";
        sink.write(done.data(), done.size());
        sink.done();
        PrintProfile();
    }
    catch (const std::exception &e)
    {
        My_Log{My_Log::Level::kError} << "raise the exception during send stream response: "
                                      << e.what() << "\n";
        return false;
    }
    return true;
}

void ResponseDispatcher::sendNormalResponse(Response &res)
{
    auto handle = model_config_.get_genie_model_handle().lock();
    std::string fullResponse;
    auto genie_callback = [this, &fullResponse, &handle](const std::string &message)
    {
        My_Log{}.original(true) << message;
        if (!isConnectionAlive())
        {
            handle->Stop();
            return false;
        }
        fullResponse += message;
        return true;
    };

    My_Log{} << "---------------Query Context Start---------------------------" << std::endl;
    handle->Query(model_input_content_, genie_callback);
    My_Log{}.original(true) << "\n";
    My_Log{} << "---------------Query Context End---------------" << std::endl;

    std::string finishReason = "stop";
    std::string ToolsResponse;
//    bool isToolResponse = str_contains(fullRespose, ResponseTools::FN_NAME);
    if (str_contains(fullResponse, ResponseTools::FN_NAME))
    {
        finishReason = "tool_calls";
        ToolsResponse = fullResponse;

        if (!model_config_.getisOutputAllText())
        {
            fullResponse = ResponseTools::remove_tool_call_content(fullResponse);
        }
        if (!fullResponse.empty())
        {
            fullResponse += "\n\n";
        }
    }

    chatHistory.AddMessage("assistant", extractFinalAnswer(fullResponse));
    auto data = ResponseTools::responseDataJson(fullResponse, finishReason, false, ToolsResponse);
    fullResponse = ResponseTools::json_to_str(data);
    res.set_content(fullResponse, MIMETYPE_JSON);
    PrintProfile();
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
                 << ", Text Length: " << model_input_content_.size()
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
        My_Log{My_Log::Level::kError} << e.what() << std::endl;
    }

    done:
    My_Log{} << "~~~~~~~~~~~~~~~Token Summary End~~~~~~~~~~~~~~~~~~~\n";
}

void ResponseDispatcher::ResetProcessor()
{
    if (proc_)
    {
        delete proc_;
        proc_ = nullptr;
    }

    switch (model_config_.get_model_style().v_)
    {
        case ModelStyle::Harmony:
            proc_ = new HarmonyProcessor{};
            break;
        default:
            proc_ = new GeneralProcessor{};
    }

    chatHistory.Clear();
}

ResponseDispatcher::~ResponseDispatcher()
{
    if (proc_)
    {
        delete proc_;
        proc_ = nullptr;
    }
}

