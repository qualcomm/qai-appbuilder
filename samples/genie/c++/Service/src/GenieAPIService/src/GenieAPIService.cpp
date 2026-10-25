//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "GenieAPIService.h"
#include <csignal>
#include <thread>
#include <chrono>
#include <iostream>
#include <string>
#include <log.h>
#include <utils.h>
#include "config.h"
#include "chat_request_handler/chat_request_handler.h"
#include "model/model_manager.h"
#include "response/response_dispatcher.h"
#include "response/response_tools_layer1_selftest.h"
#include "response/response_tools_sanitization_selftest.h"
#include "chat_request_handler/tool_call_circuit_breaker_store_selftest.h"
#include "chat_request_handler/tool_call_repetition_store_selftest.h"
#include "chat_request_handler/task_memo_doc_scan_selftest.h"
#if defined(_WIN32) || defined(__WIN32__) || defined(WIN32)
#include <windows.h>
#endif

using json = nlohmann::ordered_json;

static GenieService service;

static constexpr int kShutdownWatchdogTimeoutSeconds = 15;
static constexpr int kShutdownWatchdogExitCode = 124;

class GenieService::Route : public std::enable_shared_from_this<Route>
{
public:
    explicit Route(void (ChatRequestHandler::*func)(const httplib::Request &req, httplib::Response &res)) :
            func_{func} {}

    ~Route() = default;

    static void CreateGetRoute(const std::vector<std::string> &path,
                               void (ChatRequestHandler::*func)(const httplib::Request &req, httplib::Response &res));

    static void CreatePostRoute(const std::vector<std::string> &path,
                                void (ChatRequestHandler::*func)(const httplib::Request &req, httplib::Response &res));

protected:
    httplib::Server &(httplib::Server::*action_func_)(const std::string &, httplib::Server::Handler){};

private:
    void (ChatRequestHandler::*func_)(const httplib::Request &req, httplib::Response &res);

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
    explicit GetRoute(void (ChatRequestHandler::*func)(const httplib::Request &req, httplib::Response &res)) :
            Route(func) { action_func_ = &httplib::Server::Get; }
};

struct PostRoute : GenieService::Route
{
    explicit PostRoute(void (ChatRequestHandler::*func)(const httplib::Request &req, httplib::Response &res)) :
            Route(func) { action_func_ = &httplib::Server::Post; }
};

void GenieService::Route::CreateGetRoute(const std::vector<std::string> &path,
                                         void (ChatRequestHandler::*func)(const httplib::Request &req,
                                                                          httplib::Response &res))
{
    std::make_shared<GetRoute>(func)->Registry(path);
}

void GenieService::Route::CreatePostRoute(const std::vector<std::string> &path,
                                          void (ChatRequestHandler::*func)(const httplib::Request &req,
                                                                           httplib::Response &res))
{
    std::make_shared<PostRoute>(func)->Registry(path);
}

void GenieService::run(int argc, char *argv[])
{
    self_ = this;
    Config config{argc, argv};

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

    if (!modelManager->InitializeConfig())
    {
        My_Log{My_Log::Level::kError} << "resolve model config failed." << std::endl;
    }

    requestHandler = std::make_unique < ChatRequestHandler > (this);
    int port_checked = config.get_port();
    if (!init_)
    {
        setupSignalHandlers();
        setupHttpServer();
        init_ = true;
    }

    if (config.NeedLoadModel())
    {
        if (!modelManager->LoadSingleModel())
        {
            My_Log{My_Log::Level::kError} << "load model failed." << std::endl;
        }
    }

    const std::string &HOST = config.get_host();
    My_Log{My_Log::Level::kAlways} << YELLOW << "[OK] Genie API Service IS Running." << RESET << std::endl;
    My_Log{My_Log::Level::kAlways} << YELLOW << "[OK] Genie API Service -> http://"
                                   << HOST << ":" << port_checked
                                   << RESET
                                   << std::endl;
    svr.listen(HOST, port_checked);

    if (shutdown_requested_.load())
    {
        My_Log{} << "\n[Shutdown] Interrupt signal received. Initiating graceful shutdown...\n";
        ServiceStop();
        My_Log{} << "[Shutdown] Graceful shutdown completed.\n";
    }
    else if (!shutdown_completed_.load())
    {
        const auto wait_deadline = std::chrono::steady_clock::now() +
                std::chrono::seconds(kShutdownWatchdogTimeoutSeconds + 5);
        while (!shutdown_completed_.load() && std::chrono::steady_clock::now() < wait_deadline)
        {
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
    }
}

inline void GenieService::ServiceStop()
{
    if (stop_in_progress_.exchange(true))
    {
        while (!shutdown_completed_.load())
        {
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
        return;
    }

    My_Log{} << "start to stop service\n";
    shutdown_completed_.store(false);

    std::thread watchdog([]() {
        std::this_thread::sleep_for(std::chrono::seconds(kShutdownWatchdogTimeoutSeconds));
        if (self_ && !self_->shutdown_completed_.load())
        {
            My_Log{My_Log::Level::kError}
                    << "[Shutdown Watchdog] ServiceStop() did not complete within "
                    << kShutdownWatchdogTimeoutSeconds
                    << "s (likely a blocked inference call). Forcing process termination "
                    << "to avoid an indefinite hang." << std::endl;
#if defined(_WIN32) || defined(__WIN32__) || defined(WIN32)
            TerminateProcess(GetCurrentProcess(), static_cast<UINT>(kShutdownWatchdogExitCode));
#else
            _exit(kShutdownWatchdogExitCode);
#endif
        }
    });
    watchdog.detach();

    svr.stop();

    if (modelManager)
    {
        My_Log{} << "Unloading model...\n";
        modelManager->UnloadModel();
    }
    
    My_Log{} << "Service stopped successfully.\n";
    shutdown_completed_.store(true);
}

void GenieService::TriggerGracefulShutdown(int exit_code)
{
    if (!self_) return;
    self_->shutdown_requested_.store(true);
    self_->ServiceStop();
    My_Log{} << "Interrupt signal (" << exit_code << ") received. Exiting..." << std::endl;
    exit(exit_code);
}

#if defined(_WIN32) || defined(__WIN32__) || defined(WIN32)
namespace
{
    BOOL WINAPI ConsoleCtrlHandler(DWORD ctrl_type)
    {
        switch (ctrl_type)
        {
            case CTRL_BREAK_EVENT:
            case CTRL_CLOSE_EVENT:
            case CTRL_SHUTDOWN_EVENT:
            case CTRL_LOGOFF_EVENT:
                GenieService::TriggerGracefulShutdown(SIGINT);
                return TRUE;
            default:
                return FALSE;
        }
    }
}
#endif

inline void GenieService::setupSignalHandlers()
{
    signal(SIGINT, [](int signum) { TriggerGracefulShutdown(signum); });
#if defined(_WIN32) || defined(__WIN32__) || defined(WIN32)
    SetConsoleCtrlHandler(ConsoleCtrlHandler, TRUE);
#endif
}

void GenieService::setupHttpServer()
{
    My_Log("GenieService::setupHttpServer start\n");

    svr.set_socket_options([](socket_t sock) {
        int yes = 1;
        setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char*>(&yes), sizeof(yes));
    });

    svr.set_read_timeout(300, 0);
    svr.set_write_timeout(300, 0);
    svr.set_idle_interval(300, 0);

    svr.set_logger([](const auto &req, const httplib::Response &res)
                   {
                       if (!res.has_header("X-Skip"))
                       {
                           My_Log{} << req.path << " handling is done";
                           My_Log{}.original(true) << "\n\n";
                       }
                   });

    Route::CreateGetRoute({"/"}, &ChatRequestHandler::HandleWelcome);

    Route::CreatePostRoute({"/completions", "/v1/completions", "/chat/completions", "/v1/chat/completions"},
                           &ChatRequestHandler::ChatCompletions);

    Route::CreatePostRoute({"/textsplitter", "/v1/textsplitter"}, &ChatRequestHandler::TextSplitter);

    Route::CreateGetRoute({"/models", "/v1/models"}, &ChatRequestHandler::FetchModelList);

    Route::CreateGetRoute({"/profile"}, &ChatRequestHandler::FetchProfile);

    Route::CreateGetRoute({"/status"}, &ChatRequestHandler::FetchModelStatus);

    Route::CreatePostRoute({"/stop"}, &ChatRequestHandler::ModelStop);

    Route::CreatePostRoute({"/clear"}, &ChatRequestHandler::ClearMessage);

    Route::CreatePostRoute({"/fetch"}, &ChatRequestHandler::FetchMessage);

    Route::CreatePostRoute({"/reload"}, &ChatRequestHandler::ReloadMessage);

    Route::CreatePostRoute({"/servicestop"}, &ChatRequestHandler::ServiceExit);

    Route::CreatePostRoute({"/images/generations", "/v1/images/generations"}, &ChatRequestHandler::ImageGenerate);

    Route::CreatePostRoute({"/contextsize"}, &ChatRequestHandler::ContextSize);

    Route::CreatePostRoute({"/unload"}, &ChatRequestHandler::UnloadModel);

    My_Log("GenieService::setupHttpServer end\n");
}

int main(int argc, char **argv)
{
#if defined(_WIN32) || defined(__WIN32__) || defined(WIN32)
    SetConsoleOutputCP(CP_UTF8);
#endif
    if (argc > 1 && std::string(argv[1]) == "--self-test-layer1-recovery")
    {
        bool all_passed = RunLayer1RecoverySelfTest(std::cout);
        return all_passed ? 0 : 1;
    }
    if (argc > 1 && std::string(argv[1]) == "--self-test-bare-json-detect")
    {
        bool all_passed = RunBareToolCallDetectionSelfTest(std::cout);
        return all_passed ? 0 : 1;
    }
    if (argc > 1 && std::string(argv[1]) == "--self-test-bare-json-holdback")
    {
        bool all_passed = RunBareJsonHoldBackStreamingSelfTest(std::cout);
        return all_passed ? 0 : 1;
    }
    if (argc > 1 && std::string(argv[1]) == "--self-test-sanitization")
    {
        bool all_passed = RunToolCallSanitizationSelfTest(std::cout);
        return all_passed ? 0 : 1;
    }
    if (argc > 1 && std::string(argv[1]) == "--self-test-circuit-breaker")
    {
        bool all_passed = RunToolCallCircuitBreakerSelfTest(std::cout);
        return all_passed ? 0 : 1;
    }
    if (argc > 1 && std::string(argv[1]) == "--self-test-tool-repetition")
    {
        bool all_passed = RunToolCallRepetitionSelfTest(std::cout);
        return all_passed ? 0 : 1;
    }
    if (argc > 1 && std::string(argv[1]) == "--self-test-task-memo-scan")
    {
        bool all_passed = RunTaskMemoDocScanSelfTest(std::cout);
        return all_passed ? 0 : 1;
    }
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
