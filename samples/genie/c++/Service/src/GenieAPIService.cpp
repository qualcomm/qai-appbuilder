//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include <csignal>
#include <filesystem>
#include <httplib.h>
#include "core/utils.h"
#include "core/log.h"
#include "core/response_dispatcher.h"
#include "core/config.h"
#include "core/model/model_manager.h"

#if defined(WIN32)

#include <windows.h>

#pragma comment(lib, "ws2_32.lib")
#endif

class GenieService;

class ChatRequestHandler
{
public:
    explicit ChatRequestHandler(GenieService *srv);

    void handleWelcome(const httplib::Request &req, httplib::Response &res);

    void handleImageGenerate(const httplib::Request &req, httplib::Response &res);

    void handleChatCompletions(const httplib::Request &req, httplib::Response &res);

    void handleModelList(const httplib::Request &req, httplib::Response &res);

    void handleTextSplitter(const httplib::Request &req, httplib::Response &res);

    void handleProfile(const httplib::Request &req, httplib::Response &res);

    void handleModelStop(const httplib::Request &req, httplib::Response &res);

    void handleClearMessage(const httplib::Request &req, httplib::Response &res);

    void handleReloadMessage(const httplib::Request &req, httplib::Response &res);

    void handleFetchMessage(const httplib::Request &req, httplib::Response &res);

    void handleContextSize(const httplib::Request &req, httplib::Response &res);

    void handleServiceExit(const httplib::Request &req, httplib::Response &res);

private:
    ModelManager &modelManager;
    std::unique_ptr<ChatHistory> chatHistory;
    std::unique_ptr<ResponseDispatcher> dispatcherPtr_;
    GenieService *srv_;
};

void ChatRequestHandler::handleModelList(const httplib::Request &req, httplib::Response &res)
{
    json models = modelManager.get_model_list();
    res.set_content(models.dump(2), MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::handleContextSize(const httplib::Request &req, httplib::Response &res)
{
    json contextSize;
    contextSize["contextsize"] = modelManager.context_size();
    res.set_content(contextSize.dump(2), MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::handleModelStop(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    std::string text = data.value("text", "");
    if (text == "stop")
    {
        auto handle = modelManager.get_genie_model_handle().lock();
        handle->Stop();
    }
    res.set_content("", MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::handleClearMessage(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    std::string text = data.value("text", "");
    if (text == "Clear")
    {
        chatHistory->Clear();
    }
    My_Log{} << RED << "history message have been delete!" << RESET << std::endl;
    res.set_content("", MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::handleReloadMessage(const httplib::Request &req, httplib::Response &res)
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

void ChatRequestHandler::handleFetchMessage(const httplib::Request &req, httplib::Response &res)
{
    res.status = 200;
    res.set_content(json_to_str(chatHistory->export_to_json()), MIMETYPE_JSON);
}

void ChatRequestHandler::handleTextSplitter(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    if (!data.is_object())
    {
        res.status = 400;
        res.set_content(R"({"error": "Invalid JSON."})", MIMETYPE_JSON);
        return;
    }

    std::string text = data.value("text", "");
    int maxLength = data.value("max_length", 0);
    if (maxLength <= 0)
    {
        maxLength = modelManager.context_size() - modelManager.getminOutputNum();
    }

    std::vector<std::string> separators = data.value("separators", std::vector<std::string>{});


    auto handle = modelManager.get_genie_model_handle().lock();
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
    res.set_content(jsonData.dump(2), MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::handleChatCompletions(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    if (!data.is_object())
    {
        res.status = 400;
        res.set_content(R"({"error": "Invalid JSON."})", MIMETYPE_JSON);
        return;
    }

    std::string modelName = data.value("model", "");
    bool new_model;
    if (!modelManager.LoadModelByName(modelName, new_model))
    {
        res.status = 500;
        res.set_content(R"({"error": "Model load failed."})", MIMETYPE_JSON);
        return;
    }
    if (new_model)
        dispatcherPtr_->ResetProcessor();

    auto handle = modelManager.get_genie_model_handle().lock();

    if (!handle)
    {
        res.status = 500;
        res.set_content(R"({"error": "Model context unavailable."})", MIMETYPE_JSON);
        return;
    }

    // If it is a Lora model, then load the adapter and Lora value parameters.
    modelName = modelManager.getModelName();
    if (modelName.find("lora") != std::string::npos)
    {
        My_Log{} << "Load lora" << std::endl;
        std::unordered_map<std::string, float> loraAlphaValue{};
        loraAlphaValue["lora_alpha"] = modelManager.getloraAlpha();
        std::string engineRole{"primary"};
        handle->applyLora(engineRole, modelManager.getloraAdapter());
        handle->setLoraStrength(engineRole, loraAlphaValue);
    }

    // parsec message
    int size = modelManager.context_size();
    auto temp = std::to_string(get_json_value(data, "temp", 0.8));
    auto top_k = std::to_string(get_json_value(data, "top_k", 40));
    auto top_p = std::to_string(get_json_value(data, "top_p", 0.95));
    handle->SetParams(std::to_string(size), temp, top_k, top_p);

    /* We can configure it in 'config.json' file.
    // Set model end character
    if (str_search(modelName, "IBM-Granite"))
    {
        std::string stopSequence = "{\n  \"stop-sequence\" : [\"<|end_of_text|>\", \"<|end_of_role|>\", \"<|start_of_role|>\"]\n}";
        My_Log{} << "SetStopSequence: " << stopSequence << std::endl;
        handle->SetStopSequence(stopSequence);
    };
    */

    // Load model input information
    bool is_tool;
    std::string modelInputContent = chatHistory->BuildPrompt(data, is_tool);
    if (modelInputContent.empty())
    {
        res.status = 500;
        res.set_content(R"({"error": "Model prompt is too long."})", MIMETYPE_JSON);
        return;
    }

    dispatcherPtr_->Prepare(modelInputContent, req, is_tool);
    get_json_value(data, "stream", false) ?
    res.set_chunked_content_provider(
            "text/event-stream",
            [this](size_t offset, httplib::DataSink &sink)
            { return dispatcherPtr_->sendStreamResponse(offset, sink); },
            nullptr
    ) :
    dispatcherPtr_->sendNormalResponse(res);
}

void ChatRequestHandler::handleProfile(const httplib::Request &, httplib::Response &res)
{
    auto handle = modelManager.get_genie_model_handle().lock();
    json result = handle->HandleProfile();
    if (!result.empty())
    {
        res.set_content(json_to_str(result), MIMETYPE_JSON);
        res.status = 200;
    }
}

void ChatRequestHandler::handleImageGenerate(const Request &req, Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    res.set_content("", MIMETYPE_JSON);
    res.status = 501;
}

void ChatRequestHandler::handleWelcome(const Request &req, Response &res)
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

static class GenieService
{
public:
    void run(int argc, char *argv[]);

    void serviceStop()
    {
        My_Log{} << "start to stop service\n";
        modelManager->UnloadModel();
        svr.stop();
    }

private:
    std::atomic<bool> init_{false};
    std::unique_ptr<ModelManager> modelManager;
    httplib::Server svr;
    std::unique_ptr<ChatRequestHandler> requestHandler;
    static inline GenieService *self_;

    void setupSignalHandlers();

    void setupHttpServer();

    class Route;

    class GetRoute;

    class PostRoute;

    friend ChatRequestHandler;
    std::vector<std::shared_ptr<Route>> routes_{};
} service;

ChatRequestHandler::ChatRequestHandler(GenieService *srv) :
        modelManager(*srv->modelManager),
        chatHistory(std::make_unique<ChatHistory>(modelManager)),
        dispatcherPtr_{std::make_unique<ResponseDispatcher>(modelManager,
                                                            *chatHistory)},
        srv_{srv}
{}

void ChatRequestHandler::handleServiceExit(const Request &req, Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    std::string text = data.value("text", "");
    if (text == "stop")
    {
        My_Log{} << RED << "Service Stopping!" << RESET << std::endl;
        srv_->serviceStop();
    }
    res.set_content("", MIMETYPE_JSON);
    res.status = 200;
}

class GenieService::Route : public std::enable_shared_from_this<Route>
{
public:
    Route(bool http_block_check,
          void (ChatRequestHandler::*func)(const httplib::Request &req, httplib::Response &res)) :
            http_block_check_{http_block_check},
            func_{func}
    {}

    ~Route() = default;

    static void CreateGetRoute(const std::vector<std::string> &path,
                               void (ChatRequestHandler::*func)(const httplib::Request &req, httplib::Response &res),
                               bool http_block_check = true);

    static void CreatePostRoute(const std::vector<std::string> &path,
                                void (ChatRequestHandler::*func)(const httplib::Request &req, httplib::Response &res),
                                bool http_block_check = true);


protected:
    Server &(Server::*action_func_)(const std::string &, Server::Handler){};


private:
    void (ChatRequestHandler::*func_)(const httplib::Request &req, httplib::Response &res);

    bool http_block_check_{};

    void Registry(const std::vector<std::string> &paths)
    {
        self_->routes_.push_back(shared_from_this());
        auto route = self_->routes_.back().get();
        for (auto &path: paths)
        {
            (self_->svr.*action_func_)(path, [route, this](const httplib::Request &req, Response &res)
            {
                My_Log{} << "---------------------------------------------------\n"
                         << "Time: " << My_Log::GetTimeString() << "\n"
                         << "Path: " << req.path << std::endl;

                if (!route->http_block_check_)
                    goto ahead;

                if (http_busy_)
                {
                    My_Log{} << "An other request has been blocked." << std::endl;
                    res.set_content(R"({"error": "genie services is busy"})", MIMETYPE_JSON);
                    res.set_header("X-Skip", "1");
                    res.status = 429;
                    return;
                }
                http_busy_ = true;
                ahead:
                try
                {
                    ((*self_->requestHandler).*func_)(req, res);
                }
                catch (std::exception &e)
                {
                    My_Log{My_Log::Level::kError} << "raise the exception: " << e.what()
                                                  << " the request body: " << req.body << "\n";
                    res.set_content(R"({"error": "services error"})", MIMETYPE_JSON);
                    res.status = 500;
                }
            });
        }
    }
};

struct GenieService::GetRoute : GenieService::Route
{
    GetRoute(bool global_block,
             void (ChatRequestHandler::*func)(const httplib::Request &req, httplib::Response &res)) :
            Route(global_block, func)
    { action_func_ = &Server::Get; }
};

struct GenieService::PostRoute : GenieService::Route
{
    PostRoute(bool global_block,
              void (ChatRequestHandler::*func)(const httplib::Request &req, httplib::Response &res)) :
            Route(global_block, func)
    { action_func_ = &Server::Post; }
};

void GenieService::Route::CreateGetRoute(const std::vector<std::string> &path,
                                         void (ChatRequestHandler::*func)(const httplib::Request &req,
                                                                          httplib::Response &res),
                                         bool http_block_check)
{
    std::make_shared<GetRoute>(http_block_check, func)->Registry(path);
}

void GenieService::Route::CreatePostRoute(const std::vector<std::string> &path,
                                          void (ChatRequestHandler::*func)(const httplib::Request &req,
                                                                           httplib::Response &res),
                                          bool http_block_check)
{
    std::make_shared<PostRoute>(http_block_check, func)->Registry(path);
}

void GenieService::run(int argc, char *argv[])
{
    self_ = this;
    Config config{argc, argv};

    // 1. Parsing command line arguments
    try
    {
        if (!config.Process())
        {
            return;
        }
        modelManager = std::make_unique<ModelManager>(config.get_mode_manager_config());
    }
    catch (const std::exception &e)
    {
        My_Log{My_Log::Level::kError} << e.what() << std::endl;
        return;
    }

    if (!modelManager->InitializeConfig(config.NeedLoadModel()))
    {
        My_Log{} << "Failed to load model." << std::endl;
        return;
    }

    // Initialize request handler
    requestHandler = std::make_unique<ChatRequestHandler>(this);
    int port_checked = config.get_port();
    if (!init_)
    {
        setupSignalHandlers();
        setupHttpServer();
        init_ = true;
    }

    static const std::string HOST = "0.0.0.0";
    My_Log{My_Log::Level::kAlways} << YELLOW << "[OK] Genie API Service IS Running." << RESET << std::endl;
    My_Log{My_Log::Level::kAlways} << YELLOW << "[OK] Genie API Service -> http://"
                                   << HOST << ":" << port_checked
                                   << RESET
                                   << std::endl;
    svr.listen(HOST, port_checked);
}

void GenieService::setupSignalHandlers()
{
    signal(SIGINT, [](int signum)
    {
        service.serviceStop();
        My_Log{} << "Interrupt signal (" << signum << ") received. Exiting..." << std::endl;
        exit(signum);
    });
}

void GenieService::setupHttpServer()
{
    My_Log("GenieService::setupHttpServer start\n");
    svr.set_logger([](const auto &req, const Response &res)
                   {
                       if (!res.has_header("X-Skip"))
                       {
                           My_Log{} << req.path << " handling is done";
                           My_Log{}.original(true) << "\n\n";
                           http_busy_ = false;
                       }
                   });

    Route::CreateGetRoute({"/"}, &ChatRequestHandler::handleWelcome, false);

    Route::CreatePostRoute({"/completions", "/v1/completions", "/chat/completions", "/v1/chat/completions"},
                           &ChatRequestHandler::handleChatCompletions);

    Route::CreatePostRoute({"/textsplitter", "/v1/textsplitter"}, &ChatRequestHandler::handleTextSplitter);

    Route::CreateGetRoute({"/models", "/v1/models"}, &ChatRequestHandler::handleModelList);

    Route::CreateGetRoute({"/profile"}, &ChatRequestHandler::handleProfile);

    Route::CreatePostRoute({"/stop"}, &ChatRequestHandler::handleModelStop);

    Route::CreatePostRoute({"/Clear"}, &ChatRequestHandler::handleClearMessage);

    Route::CreatePostRoute({"/fetch"}, &ChatRequestHandler::handleFetchMessage);

    Route::CreatePostRoute({"/reload"}, &ChatRequestHandler::handleReloadMessage);

    Route::CreatePostRoute({"/servicestop"}, &ChatRequestHandler::handleServiceExit, false);

    Route::CreatePostRoute({"/images/generations", "/v1/images/generations"}, &ChatRequestHandler::handleImageGenerate);

    Route::CreatePostRoute({"/contextsize"}, &ChatRequestHandler::handleContextSize);

    My_Log("GenieService::setupHttpServer end\n");
}

int main(int argc, char **argv)
{
#if defined(_WIN32) || defined(__WIN32__) || defined(WIN32)
    SetConsoleOutputCP(CP_UTF8);
#endif
    service.run(argc, argv);
    return 0;
}

#ifdef __ANDROID__
#include <jni.h>

extern "C" JNIEXPORT void JNICALL
Java_com_example_genieapiservice_MyNativeLib_runService(JNIEnv *env, jobject /* this */, jobjectArray args) {
    int argc = env->GetArrayLength(args);
    std::vector<char*> argv;

    My_Log("MyNativeLib_runService argc: " + std::to_string(argc) + "\n");

    for (int i = 0; i < argc; ++i) {
        jstring arg = (jstring)env->GetObjectArrayElement(args, i);
        const char* c_str = env->GetStringUTFChars(arg, nullptr);
        My_Log("MyNativeLib_runService argv: " + std::string(c_str) + "\n");
        argv.push_back(const_cast<char*>(c_str));
    }

    service.run(argc, argv.data());
    My_Log("MyNativeLib_runService down\n");

    for (int i = 0; i < argc; ++i) {
        jstring arg = (jstring)env->GetObjectArrayElement(args, i);
        env->ReleaseStringUTFChars(arg, argv[i]);
    }
}

extern "C" JNIEXPORT void JNICALL
Java_com_example_genieapiservice_MyNativeLib_stopService(JNIEnv *env, jobject /* this */) {
    service.serviceStop();
}
#endif
