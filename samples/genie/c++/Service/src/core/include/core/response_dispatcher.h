#ifndef GENIEAPICLIENT_SLN_RESPONSE_DISPATCHER_H
#define GENIEAPICLIENT_SLN_RESPONSE_DISPATCHER_H

#include "httplib.h"
#include "core/context_base.h"
#include "processor.h"
#include "core/chat_history.h"
#include "core/model/model_manager.h"

using namespace httplib;

class ResponseDispatcher
{
public:
    ResponseDispatcher(ModelManager& model_mgr,
                       ChatHistory &chatHistory);

    ~ResponseDispatcher();

    void ResetProcessor();

    void Prepare(const std::string &modelInputContent,
                 const httplib::Request &req,
                 bool is_tool);

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
    {
        return proc_->preprocessStream(chunkText, isToolResponse, toolResponse);
    }

    bool is_tool_{};
    ChatHistory &chatHistory;
    IModelConfig& model_config_;
    std::string response_buffer; // The response buffer is used to store the response content of the model.

    std::string model_input_content_;
    httplib::Request *req_{};
    ModelProcessor *proc_{};
};

#endif //GENIEAPICLIENT_SLN_RESPONSE_DISPATCHER_H
