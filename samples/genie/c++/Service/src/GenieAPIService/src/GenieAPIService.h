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

class ModelManager;
class ChatRequestHandler;

class GenieService
{
public:
    void run(int argc, char *argv[]);

    void ServiceStop();

    class Route;

private:
    std::atomic<bool> init_{false};
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
