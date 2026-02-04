//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "chat_request_handler.h"
#include "utils.h"
#include "log.h"
#include "model_input_builder.h"
#include "text_splitter.h"
#include "../GenieAPIService.h"
#include "../response/response_dispatcher.h"

ChatRequestHandler::ChatRequestHandler(GenieService *srv) :
        model_manager(*srv->modelManager),
        chatHistory(std::make_unique < ChatHistory > (model_manager)),
        dispatcherPtr_{std::make_unique < ResponseDispatcher > (model_manager, *chatHistory)},
        input_builder_{new ModelInputBuilder(*chatHistory, model_manager)},
        srv_{srv} {}

ChatRequestHandler::~ChatRequestHandler()
{
    delete input_builder_;
}

void ChatRequestHandler::FetchModelList(const httplib::Request &req, httplib::Response &res)
{
    json models = model_manager.get_model_list();
    res.set_content(models.dump(2), ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::ContextSize(const httplib::Request &req, httplib::Response &res)
{
    json contextSize;
    contextSize["contextsize"] = model_manager.context_size();
    res.set_content(contextSize.dump(2), ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::ServiceExit(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    std::string text = data.value("text", "");
    if (text == "stop")
        srv_->ServiceStop();

    res.set_content("", ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::ModelStop(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    std::string text = data.value("text", "");
    if (text == "stop")
    {
        auto handle = model_manager.get_genie_model_handle().lock();
        handle->Stop();
    }
    res.set_content("", ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::ClearMessage(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    std::string text = data.value("text", "");
    if (text == "clear")
    {
        chatHistory->Clear();
    }
    My_Log{} << RED << "history message have been delete!" << RESET << std::endl;
    res.set_content("", ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::ReloadMessage(const httplib::Request &req, httplib::Response &res)
{
    auto j = json::parse(req.body, nullptr, false);
    if (chatHistory->import_from_json(j))
    {
        res.set_content("{\"status\": \"success\"}", "application/json");
    }
    else
    {
        res.status = 400;
        res.set_content("{\"error\": \"Invalid history format\"}", "application/json");
    }
}

void ChatRequestHandler::FetchMessage(const httplib::Request &req, httplib::Response &res)
{
    res.status = 200;
    res.set_content(json_to_str(chatHistory->export_to_json()), ResponseDispatcher::MIMETYPE_JSON);
}

void ChatRequestHandler::TextSplitter(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    if (!data.is_object())
    {
        res.status = 400;
        res.set_content(R"({"error": "Invalid JSON."})", ResponseDispatcher::MIMETYPE_JSON);
        return;
    }

    std::string text = data.value("text", "");
    int maxLength = data.value("max_length", 0);
    if (maxLength <= 0)
    {
        maxLength = model_manager.context_size() - model_manager.getminOutputNum();
    }

    std::vector<std::string> separators = data.value("separators", std::vector<std::string>{});
    auto handle = model_manager.get_genie_model_handle().lock();
    auto lengthFn = [&handle](const std::string &s)
    {
        return handle->TokenLength(s);
    };

    static const std::vector<std::string> &SEPARATORS = {"\n\n", "\n", "。", "！", "？", "，", ".", "?", "!", ",", " ", ""};
    if (separators.empty())
    {
        separators = SEPARATORS;
    }
    RecursiveCharacterTextSplitter splitter(separators, true, maxLength, lengthFn);
    auto chunks = splitter.split_text(text);

    json jsonData;
    std::vector<json> content;

    for (const auto &item: chunks)
    {
        json item_json;
        item_json["text"] = item;
        item_json["length"] = handle->TokenLength(item);
        content.push_back(item_json);
    }
    jsonData["content"] = content;
    jsonData["object"] = "list";
    res.set_content(jsonData.dump(2), ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::ChatCompletions(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    if (!data.is_object())
    {
        res.status = 400;
        res.set_content(R"({"error": "Invalid JSON."})", ResponseDispatcher::MIMETYPE_JSON);
        return;
    }

    std::string modelName = data.value("model", "");
    bool new_model;
    if (!model_manager.LoadModelByName(modelName, new_model))
    {
        res.status = 500;
        res.set_content(R"({"error": "Model load failed."})", ResponseDispatcher::MIMETYPE_JSON);
        return;
    }

    if (new_model)
        dispatcherPtr_->ResetProcessor();

    auto handle = model_manager.get_genie_model_handle().lock();
    if (!handle)
    {
        res.status = 500;
        res.set_content(R"({"error": "Model context unavailable."})", ResponseDispatcher::MIMETYPE_JSON);
        return;
    }

    if (modelName.find("lora") != std::string::npos)
    {
        std::unordered_map<std::string, float> loraAlphaValue
                {
                        {"lora_alpha", model_manager.getloraAlpha()}
                };
        const char *engineRole{"primary"};
        handle->applyLora(engineRole, model_manager.getloraAdapter());
        handle->setLoraStrength(engineRole, loraAlphaValue);
    }

    handle->SetParams(std::to_string(get_json_value(data, "size", model_manager.context_size())),
                      std::to_string(get_json_value(data, "temp", 0.8)),
                      std::to_string(get_json_value(data, "top_k", 40)),
                      std::to_string(get_json_value(data, "top_p", 0.95)));

    bool is_tool;
    auto &model_input = input_builder_->Build(data, is_tool);
    bool is_stream = get_json_value(data, "stream", false);
    dispatcherPtr_->Prepare(model_input, is_tool, is_stream, req);

    is_stream
    ? res.set_chunked_content_provider(
            "text/event-stream",
            [this](size_t offset, httplib::DataSink &sink) { return dispatcherPtr_->SendResponse(offset, &sink, nullptr); },
            nullptr
    )
    : static_cast<void>(dispatcherPtr_->SendResponse(0, nullptr, &res));
}

void ChatRequestHandler::FetchProfile(const httplib::Request &req, httplib::Response &res)
{
    auto handle = model_manager.get_genie_model_handle().lock();
    json result = handle->HandleProfile();
    if (!result.empty())
    {
        res.set_content(json_to_str(result), ResponseDispatcher::MIMETYPE_JSON);
        res.status = 200;
    }
}

void ChatRequestHandler::ImageGenerate(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    res.set_content("", ResponseDispatcher::MIMETYPE_JSON);
    res.status = 501;
}

void ChatRequestHandler::HandleWelcome(const httplib::Request &req, httplib::Response &res)
{
    static const auto root_html = R"(
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="UTF-8">
    <title>Genie API Service</title>
    <style>
    body { word-wrap: break-word; white-space: normal; }
    h1 {text-align: center;}
    </style>
    </head>
    <body>
    <br><br>
    <h1>Genie API Service IS Running.</h1>
    </body>
    </html>
    )";
    res.set_content(root_html, "text/html");
}

void ChatRequestHandler::FetchModelStatus(const httplib::Request &req, httplib::Response &res)
{
    json result;
    result["loading"] = std::to_string(!model_manager.IsLoaded());
    res.set_content(result, ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::UnloadModel(const httplib::Request &req, httplib::Response &res)
{
    model_manager.UnloadModel();
    res.set_content("", ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}
