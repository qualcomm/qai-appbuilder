//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef GENIEAPISERVICE_H
#define GENIEAPISERVICE_H

#include <httplib.h>
#include <memory>
#include <atomic>
#include <mutex>
#include <vector>

class ModelManager;
class ChatRequestHandler;

class GenieService
{
public:
    int run(int argc, char *argv[]);

    void ServiceStop();

    static void TriggerGracefulShutdown(int exit_code);

    class Route;

private:
    std::atomic<bool> init_{false};
    std::atomic<bool> shutdown_requested_{false};
    std::atomic<bool> shutdown_completed_{false};
    std::atomic<bool> stop_in_progress_{false};
    std::atomic<bool> listening_{false};
    std::atomic<int> exit_code_{0};
    std::atomic<unsigned> stop_epoch_{0};
    std::unique_ptr<ModelManager> modelManager;
    httplib::Server svr;
    std::mutex request_handler_mtx_;
    std::shared_ptr<ChatRequestHandler> requestHandler;
    static inline GenieService *self_;

    void setupSignalHandlers();

    void setupHttpServer();

    friend ChatRequestHandler;
    std::vector<std::shared_ptr<Route> > routes_{};
};

#endif
