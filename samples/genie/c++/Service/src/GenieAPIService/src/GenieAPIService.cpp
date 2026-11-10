//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "GenieAPIService.h"
#include <csignal>
#include <cerrno>
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
#if defined(_WIN32) || defined(__WIN32__) || defined(WIN32)
#include <windows.h>
#endif

using json = nlohmann::ordered_json;

static GenieService service;

static constexpr int kShutdownWatchdogTimeoutSeconds = 15;
static constexpr int kShutdownWatchdogExitCode = 124;

namespace
{
    volatile std::sig_atomic_t g_pending_signal = 0;

    std::string Utf8PreviewPrefix(const std::string &body, size_t max_bytes)
    {
        if (body.size() <= max_bytes)
        {
            return body;
        }
        size_t cut = max_bytes;
        while (cut > 0 && (static_cast<unsigned char>(body[cut]) & 0xC0) == 0x80)
        {
            --cut;
        }
        return body.substr(0, cut);
    }
}

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
                ErrorHandle error_handle;
                try
                {
                    std::shared_ptr<ChatRequestHandler> handler;
                    {
                        std::lock_guard<std::mutex> lock(self_->request_handler_mtx_);
                        handler = self_->requestHandler;
                    }
                    if (!handler)
                    {
                        res.set_content(R"({"error": "services error"})", ResponseDispatcher::MIMETYPE_JSON);
                        res.status = 503;
                        return;
                    }
                    ((*handler).*func_)(req, res);
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

int GenieService::run(int argc, char *argv[])
{
    self_ = this;
    stop_epoch_.fetch_add(1);
    stop_in_progress_.store(false);
    shutdown_requested_.store(false);
    shutdown_completed_.store(false);
    exit_code_.store(0);
    Config config{argc, argv};

    try
    {
        if (!config.Process())
        {
            return 0;
        }
        modelManager = std::make_unique < ModelManager > (config.get_mode_manager_config());
    }
    catch (const std::exception &e)
    {
        My_Log{My_Log::Level::kError} << e.what() << std::endl;
        return 0;
    }

    if (!modelManager->InitializeConfig())
    {
        My_Log{My_Log::Level::kError} << "resolve model config failed." << std::endl;
    }

    {
        auto new_handler = std::make_shared<ChatRequestHandler>(this);
        std::lock_guard<std::mutex> lock(request_handler_mtx_);
        requestHandler = std::move(new_handler);
    }
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
    bool stopped_by_main = false;
    bool listen_ok = true;
    listening_.store(true);
    if (g_pending_signal != 0)
    {
        g_pending_signal = 0;
        stopped_by_main = true;
        shutdown_requested_.store(true);
    }
    if (!shutdown_requested_.load())
    {
        listen_ok = svr.listen(HOST, port_checked);
    }
    listening_.store(false);

    if (!listen_ok && !shutdown_requested_.load() && !stop_in_progress_.load())
    {
        My_Log{My_Log::Level::kError} << "failed to listen on " << HOST << ":" << port_checked
                                      << ", errno: " << errno << std::endl;
        exit_code_.store(1);
        ServiceStop();
        return 1;
    }

    if (shutdown_requested_.load())
    {
        My_Log{} << "\n[Shutdown] Interrupt signal received. Initiating graceful shutdown...\n";
        ServiceStop();
        My_Log{} << "[Shutdown] Graceful shutdown completed.\n";
#if !(defined(_WIN32) || defined(__WIN32__) || defined(WIN32))
        if (!stopped_by_main)
        {
            const auto exit_deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
            while (std::chrono::steady_clock::now() < exit_deadline)
            {
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            }
        }
#endif
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
    return 0;
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

    shutdown_completed_.store(false);

    const unsigned epoch = stop_epoch_.load();
    std::thread watchdog([epoch]() {
        std::this_thread::sleep_for(std::chrono::seconds(kShutdownWatchdogTimeoutSeconds));
        if (self_ && self_->stop_epoch_.load() == epoch && !self_->shutdown_completed_.load())
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

    My_Log{} << "start to stop service\n";

    svr.stop();

    if (modelManager)
    {
        My_Log{} << "Unloading model...\n";
        modelManager->UnloadModel();
    }

    My_Log{} << "Service stopped successfully." << std::endl;
    shutdown_completed_.store(true);
#if (defined(_WIN32) || defined(__WIN32__) || defined(WIN32)) && !defined(GENIEAPI_EXPORTS)
    std::cout.flush();
    std::cerr.flush();
    fflush(nullptr);
    TerminateProcess(GetCurrentProcess(), static_cast<UINT>(exit_code_.load()));
#endif
}

void GenieService::TriggerGracefulShutdown(int exit_code)
{
    if (!self_) return;
    self_->shutdown_requested_.store(true);
    self_->ServiceStop();
    My_Log{} << "Interrupt signal (" << exit_code << ") received. Exiting..." << std::endl;
#if defined(_WIN32) || defined(__WIN32__) || defined(WIN32)
    exit(exit_code);
#else
    std::cout.flush();
    std::cerr.flush();
    fflush(nullptr);
    _exit(exit_code);
#endif
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
#if defined(_WIN32) || defined(__WIN32__) || defined(WIN32)
    signal(SIGINT, [](int signum) { TriggerGracefulShutdown(signum); });
    SetConsoleCtrlHandler(ConsoleCtrlHandler, TRUE);
#else
    signal(SIGINT, [](int signum) { g_pending_signal = signum; });
    std::thread([]() {
        for (;;)
        {
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            const int signum = g_pending_signal;
            if (signum != 0 && self_ && self_->listening_.load())
            {
                g_pending_signal = 0;
                TriggerGracefulShutdown(signum);
                return;
            }
        }
    }).detach();
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

    svr.set_pre_routing_handler([](const httplib::Request &req, httplib::Response &) -> httplib::Server::HandlerResponse
    {
        std::string block = "---------------------------------------------------\n";
        block += "Time: " + My_Log::GetTimeString() + "\n";
        block += "Path: " + req.path + "\n";
        block += "Method: " + req.method + " Target: " + req.target + "\n";
        block += "From: " + req.remote_addr + ":" + std::to_string(req.remote_port) + "\n";
        static constexpr const char *kLoggedHeaders[] = {
                "Origin", "Content-Type", "Content-Length", "User-Agent",
                "Access-Control-Request-Method", "Access-Control-Request-Headers"};
        for (const char *name: kLoggedHeaders)
        {
            if (req.has_header(name))
            {
                block += std::string(name) + ": " + req.get_header_value(name) + "\n";
            }
        }
        My_Log{} << block << std::endl;
        return httplib::Server::HandlerResponse::Unhandled;
    });

    svr.set_logger([](const auto &req, const httplib::Response &res)
                   {
                       if (!res.has_header("X-Skip"))
                       {
                           std::string line = req.method + " " + req.path + " -> " + std::to_string(res.status) + ", ";
                           if (req.matched_route.empty())
                           {
                               line += "no route matched";
                               if (!req.body.empty())
                               {
                                   line += "\nbody (" + std::to_string(req.body.size()) + " bytes): " +
                                           Utf8PreviewPrefix(req.body, 256);
                               }
                           }
                           else
                           {
                               line += "route " + req.matched_route;
                           }
                           line += " handling is done";
                           My_Log{} << line;
                           My_Log{}.original(true) << "\n\n";
                       }
                   });

    svr.set_default_headers({{"Access-Control-Allow-Origin", "*"},
                             {"Access-Control-Expose-Headers", "*"}});

    svr.Options(R"(.*)", [](const httplib::Request &req, httplib::Response &res)
    {
        res.set_header("Access-Control-Allow-Methods",
                       req.get_header_value("Access-Control-Request-Method", "GET, POST, OPTIONS"));
        res.set_header("Access-Control-Allow-Headers",
                       req.get_header_value("Access-Control-Request-Headers", "*"));
        if (req.has_header("Access-Control-Request-Private-Network"))
        {
            res.set_header("Access-Control-Allow-Private-Network", "true");
        }
        res.set_header("Access-Control-Max-Age", "86400");
        res.status = 204;
    });

    Route::CreateGetRoute({"/"}, &ChatRequestHandler::HandleWelcome);

    Route::CreatePostRoute({"/completions", "/v1/completions", "/v2/completions",
                            "/chat/completions", "/v1/chat/completions", "/v2/chat/completions"},
                           &ChatRequestHandler::ChatCompletions);

    Route::CreatePostRoute({"/textsplitter", "/v1/textsplitter", "/v2/textsplitter"},
                           &ChatRequestHandler::TextSplitter);

    Route::CreateGetRoute({"/models", "/v1/models", "/v2/models"}, &ChatRequestHandler::FetchModelList);

    Route::CreateGetRoute({"/profile", "/v1/profile", "/v2/profile"}, &ChatRequestHandler::FetchProfile);

    Route::CreateGetRoute({"/status", "/v1/status", "/v2/status"}, &ChatRequestHandler::FetchModelStatus);

    Route::CreatePostRoute({"/stop", "/v1/stop", "/v2/stop"}, &ChatRequestHandler::ModelStop);

    Route::CreatePostRoute({"/clear", "/v1/clear", "/v2/clear"}, &ChatRequestHandler::ClearMessage);

    Route::CreatePostRoute({"/fetch", "/v1/fetch", "/v2/fetch"}, &ChatRequestHandler::FetchMessage);

    Route::CreatePostRoute({"/reload", "/v1/reload", "/v2/reload"}, &ChatRequestHandler::ReloadMessage);

    Route::CreatePostRoute({"/servicestop", "/v1/servicestop", "/v2/servicestop"},
                           &ChatRequestHandler::ServiceExit);

    Route::CreatePostRoute({"/images/generations", "/v1/images/generations", "/v2/images/generations"},
                           &ChatRequestHandler::ImageGenerate);

    Route::CreatePostRoute({"/contextsize", "/v1/contextsize", "/v2/contextsize"},
                           &ChatRequestHandler::ContextSize);

    Route::CreatePostRoute({"/unload", "/v1/unload", "/v2/unload"}, &ChatRequestHandler::UnloadModel);

    Route::CreatePostRoute({"/v2/unload_all"}, &ChatRequestHandler::UnloadAllModels);

    My_Log("GenieService::setupHttpServer end\n");
}

int main(int argc, char **argv)
{
#if defined(_WIN32) || defined(__WIN32__) || defined(WIN32)
    SetConsoleOutputCP(CP_UTF8);
#endif
    return service.run(argc, argv);
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
