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
#include <log.h>
#include <utils.h>
#include "config.h"
#include "chat_request_handler/chat_request_handler.h"
#include "model/model_manager.h"
#include "response/response_dispatcher.h"
#if defined(_WIN32) || defined(__WIN32__) || defined(WIN32)
#include <windows.h>
#endif

using json = nlohmann::ordered_json;

static GenieService service;

// 关闭看门狗兜底阈值：明显小于测试脚本判定"挂起"的 30s 窗口，正常关闭远快于该阈值，
// 不影响任何正常关闭场景；仅在底层推理调用（如 MNN 在真实内存压力下的 generate()）
// 未能在 ModelManager::UnloadModel() 主动调用的 Stop() 信号下及时让出控制权时兜底，
// 把"无限期挂起"转化为"有限时间后被强制终止"。
static constexpr int kShutdownWatchdogTimeoutSeconds = 15;
// 看门狗强制终止时使用的退出码：刻意选用一个不落在 test_service.py
// _WINDOWS_EXIT_CODE_HINTS 崩溃特征码表内的普通正数，配合下方的专属日志标记，
// 使测试报告能把"看门狗兜底退出"与"真实崩溃(NTSTATUS 特征码)"清楚区分开，
// 不会被误判为崩溃。
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

    // InitializeConfig must complete before ChatRequestHandler construction
    // because the handler reads routing/cloud config set during initialization.
    if (!modelManager->InitializeConfig(config.NeedLoadModel()))
    {
        My_Log{My_Log::Level::kError} << "load model failed." << std::endl;
    }

    // Initialize request handler and start HTTP server BEFORE model loading.
    // This allows /models and /status endpoints to respond immediately,
    // enabling remote test scripts to detect connectivity while models load in background.
    requestHandler = std::make_unique < ChatRequestHandler > (this);
    int port_checked = config.get_port();
    if (!init_)
    {
        setupSignalHandlers();
        setupHttpServer();
        init_ = true;
    }

    // Load additional models from service_config.json only when the user explicitly
    // requested model loading via -l/--load_model.  Without -l the service starts
    // with only the primary model specified by -c, and no extra models are loaded
    // automatically.
    if (config.NeedLoadModel())
    {
        std::thread model_loader([this]() {
            modelManager->LoadAllModelsFromConfig();
        });
        model_loader.detach();
    }

    static const std::string HOST = "0.0.0.0";
    My_Log{My_Log::Level::kAlways} << YELLOW << "[OK] Genie API Service IS Running." << RESET << std::endl;
    My_Log{My_Log::Level::kAlways} << YELLOW << "[OK] Genie API Service -> http://"
                                   << HOST << ":" << port_checked
                                   << RESET
                                   << std::endl;
    svr.listen(HOST, port_checked);
    
    // 服务器停止后（可能是由于 Ctrl+C 信号），执行清理
    if (shutdown_requested_.load())
    {
        My_Log{} << "\n[Shutdown] Interrupt signal received. Initiating graceful shutdown...\n";
        ServiceStop();
        My_Log{} << "[Shutdown] Graceful shutdown completed.\n";
    }
}

inline void GenieService::ServiceStop()
{
    My_Log{} << "start to stop service\n";
    shutdown_completed_.store(false);

    // 关闭看门狗：detach 一个线程，若 ServiceStop() 主体逻辑在阈值内仍未完成（即
    // shutdown_completed_ 仍是 false），说明底层某个调用（最典型是 MNN 在内存压力下
    // 的 generate()）卡住了，主动强制终止整个进程，保证"收到终止信号后总能在有限
    // 时间内退出"这条底线，即使 ModelManager::UnloadModel() 里新增的 Stop() 信号未能
    // 让第三方推理库的阻塞调用真正让出控制权。正常关闭路径远快于该阈值，不受影响。
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

    // Stop accepting new connections immediately so that connectivity checks
    // from test scripts fail, signaling that the service is truly down.
    svr.stop();
    
    // Then unload model to release hardware (NPU/GPU) resources.
    // NPU 释放等待已下沉到 ModelManager::Clean() 内部（UnloadModel() 经过此处），
    // 这里不再重复等待，避免与 GenieAPILibrary::api_unloadmodel() 等其他调用路径行为不一致。
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
    // signal(SIGINT, ...) below only reacts to Ctrl+C. An orchestrator that needs to target
    // just this process (without affecting siblings sharing the same console) can only use
    // CTRL_BREAK_EVENT on Windows, but that event never raises SIGINT via the CRT - without
    // this handler the OS force-terminates the process (STATUS_CONTROL_C_EXIT) before any of
    // our shutdown/unload code runs. Route it through the same TriggerGracefulShutdown path.
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
    
    // Allow rapid port rebind after service restart (avoids TIME_WAIT blocking)
    svr.set_socket_options([](socket_t sock) {
        int yes = 1;
        setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char*>(&yes), sizeof(yes));
    });

    // 设置合理的超时时间，避免因模型推理耗时过长导致连接主动断开 (任务2)
    svr.set_read_timeout(300, 0);   // 300 秒读超时
    svr.set_write_timeout(300, 0);  // 300 秒写超时
    svr.set_idle_interval(300, 0);  // 300 秒空闲保持
    
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
