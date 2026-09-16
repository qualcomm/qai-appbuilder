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

class ModelManager;
class ChatRequestHandler;

class GenieService
{
public:
    void run(int argc, char *argv[]);

    void ServiceStop();

    static void TriggerGracefulShutdown(int exit_code);

    class Route;

private:
    std::atomic<bool> init_{false};
    std::atomic<bool> shutdown_requested_{false};
    // ServiceStop() 主体逻辑是否已经跑完；由关闭看门狗线程读取，用于判断是否需要
    // 在超时后强制终止进程，避免底层推理调用（如 MNN 在内存压力下的 generate()）
    // 阻塞导致优雅关闭路径无限期挂起。
    std::atomic<bool> shutdown_completed_{false};
    // ServiceStop() 幂等性保护：Ctrl+C 路径下 TriggerGracefulShutdown() 会同步调用一次
    // ServiceStop()，随后 run() 里 svr.listen() 因 svr.stop() 提前返回、且此时
    // shutdown_requested_ 已置位，会再次调用 ServiceStop()——两次调用可能在不同线程上
    // 并发执行，各自触发一次 modelManager->UnloadModel()。该标志确保核心卸载逻辑
    // 全进程只真正执行一次；后来的调用只等待第一次调用完成后直接返回。
    std::atomic<bool> stop_in_progress_{false};
    std::unique_ptr<ModelManager> modelManager;
    httplib::Server svr;
    std::unique_ptr<ChatRequestHandler> requestHandler;
    static inline GenieService *self_;

    void setupSignalHandlers();

    void setupHttpServer();

    friend ChatRequestHandler;
    std::vector<std::shared_ptr<Route> > routes_{};
};

#endif //GENIEAPISERVICE_H
