//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "GenieAPIService.h"
#include <csignal>
#include <httplib.h>
#include "log.h"
#include "config.h"
#include "utils.h"
#include "chat_request_handler/chat_request_handler.h"
#include "model/model_manager.h"
#include "response/response_dispatcher.h"

static GenieService service;

class GenieService::Route : public std::enable_shared_from_this<Route>
{
public:
    Route(bool http_block_check,
          void (ChatRequestHandler::*func)(const httplib::Request &req, httplib::Response &res)) :
            func_{func},
            http_block_check_{http_block_check} {}

    ~Route() = default;

    static void CreateGetRoute(const std::vector<std::string> &path,
                               void (ChatRequestHandler::*func)(const httplib::Request &req, httplib::Response &res),
                               bool http_block_check = true);

    static void CreatePostRoute(const std::vector<std::string> &path,
                                void (ChatRequestHandler::*func)(const httplib::Request &req, httplib::Response &res),
                                bool http_block_check = true);

protected:
    httplib::Server &(httplib::Server::*action_func_)(const std::string &, httplib::Server::Handler){};

private:
    void (ChatRequestHandler::*func_)(const httplib::Request &req, httplib::Response &res);

    bool http_block_check_{};

    struct ErrorHandle
    {
        const char *msg_;
        int status_;
        std::string internal_msg_;
    };

    void Registry(const std::vector<std::string> &paths)
    {
        self_->routes_.push_back(shared_from_this());
        auto route = self_->routes_.back().get();
        for (auto &path: paths)
        {
            (self_->svr.*action_func_)(path, [route, this](const httplib::Request &req, httplib::Response &res)
            {
                My_Log{} << "---------------------------------------------------\n"
                         << "Time: " << My_Log::GetTimeString() << "\n"
                         << "Path: " << req.path << std::endl;

                My_Log{My_Log::Level::kInfo} << req.body << "\n";
                if (!route->http_block_check_)
                    goto ahead;

                if (http_busy_)
                {
                    My_Log{} << "An other request has been blocked." << std::endl;
                    res.set_content(R"({"error": "genie services is busy"})", ResponseDispatcher::MIMETYPE_JSON);
                    res.set_header("X-Skip", "1");
                    res.status = 429;
                    return;
                }
                http_busy_ = true;

                ahead:
                ErrorHandle error_handle;
                try
                {
                    ((*self_->requestHandler).*func_)(req, res);
                    return;
                }
                catch (const ReportError &e)
                {
                    error_handle = {R"({"error": "invalid operation"})", 400, e.what()};
                }
                catch (const json::exception &e)
                {
                    error_handle = {R"({"error": "invalid json"})", 400, e.what()};
                }
                catch (const std::exception &e)
                {
                    error_handle = {R"({"error": "services error"})", 500, e.what()};
                }
                My_Log{My_Log::Level::kError}
                        << "raise the exception: " << error_handle.internal_msg_ << "\n"
                        << "the request body: " << req.body << "\n";
                res.set_content(error_handle.msg_, ResponseDispatcher::MIMETYPE_JSON);
                res.status = error_handle.status_;
            });
        }
    }
};

struct GetRoute : GenieService::Route
{
    GetRoute(bool global_block,
             void (ChatRequestHandler::*func)(const httplib::Request &req, httplib::Response &res)) :
            Route(global_block, func) { action_func_ = &httplib::Server::Get; }
};

struct PostRoute : GenieService::Route
{
    PostRoute(bool global_block,
              void (ChatRequestHandler::*func)(const httplib::Request &req, httplib::Response &res)) :
            Route(global_block, func) { action_func_ = &httplib::Server::Post; }
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
        modelManager = std::make_unique < ModelManager > (config.get_mode_manager_config());
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
    requestHandler = std::make_unique < ChatRequestHandler > (this);
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

void GenieService::ServiceStop()
{
    My_Log{} << "start to stop service\n";
    modelManager->UnloadModel();
    svr.stop();
}

void GenieService::setupSignalHandlers()
{
    signal(SIGINT, [](int signum)
    {
        service.ServiceStop();
        My_Log{} << "Interrupt signal (" << signum << ") received. Exiting..." << std::endl;
        exit(signum);
    });
}

void GenieService::setupHttpServer()
{
    My_Log("GenieService::setupHttpServer start\n");
    svr.set_logger([](const auto &req, const httplib::Response &res)
                   {
                       if (!res.has_header("X-Skip"))
                       {
                           My_Log{} << req.path << " handling is done";
                           My_Log{}.original(true) << "\n\n";
                           http_busy_ = false;
                       }
                   });

    Route::CreateGetRoute({"/"}, &ChatRequestHandler::HandleWelcome, false);

    Route::CreatePostRoute({"/completions", "/v1/completions", "/chat/completions", "/v1/chat/completions"},
                           &ChatRequestHandler::ChatCompletions);

    Route::CreatePostRoute({"/textsplitter", "/v1/textsplitter"}, &ChatRequestHandler::TextSplitter);

    Route::CreateGetRoute({"/models", "/v1/models"}, &ChatRequestHandler::FetchModelList);

    Route::CreateGetRoute({"/profile"}, &ChatRequestHandler::FetchProfile);

    Route::CreateGetRoute({"/status"}, &ChatRequestHandler::FetchModelStatus, false);

    Route::CreatePostRoute({"/stop"}, &ChatRequestHandler::ModelStop, false);

    Route::CreatePostRoute({"/clear"}, &ChatRequestHandler::ClearMessage);

    Route::CreatePostRoute({"/fetch"}, &ChatRequestHandler::FetchMessage);

    Route::CreatePostRoute({"/reload"}, &ChatRequestHandler::ReloadMessage);

    Route::CreatePostRoute({"/servicestop"}, &ChatRequestHandler::ServiceExit, false);

    Route::CreatePostRoute({"/images/generations", "/v1/images/generations"}, &ChatRequestHandler::ImageGenerate);

    Route::CreatePostRoute({"/contextsize"}, &ChatRequestHandler::ContextSize);

    Route::CreatePostRoute({"/unload"}, &ChatRequestHandler::UnloadModel, false);

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
Java_com_example_genieapiservice_MyNativeLib_runService(JNIEnv *env, jobject /* this */, jobjectArray args)
{
    int argc = env->GetArrayLength(args);
    std::vector<char *> argv;

    My_Log("MyNativeLib_runService argc: " + std::to_string(argc) + "\n", My_Log::Level::kAlways);

    for (int i = 0; i < argc; ++i)
    {
        jstring arg = (jstring) env->GetObjectArrayElement(args, i);
        const char *c_str = env->GetStringUTFChars(arg, nullptr);
        My_Log("MyNativeLib_runService argv: " + std::string(c_str) + "\n");
        argv.push_back(const_cast<char *>(c_str));
    }

    service.run(argc, argv.data());
    My_Log("MyNativeLib_runService down\n", My_Log::Level::kAlways);

    for (int i = 0; i < argc; ++i)
    {
        jstring arg = (jstring) env->GetObjectArrayElement(args, i);
        env->ReleaseStringUTFChars(arg, argv[i]);
    }
}

extern "C" JNIEXPORT void JNICALL
Java_com_example_genieapiservice_MyNativeLib_stopService(JNIEnv *env, jobject /* this */) { service.ServiceStop(); }
#endif
