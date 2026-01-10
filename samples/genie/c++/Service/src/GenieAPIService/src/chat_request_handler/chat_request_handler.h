//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef CHAT_REQUEST_HANDLER_H
#define CHAT_REQUEST_HANDLER_H

#include <httplib.h>
#include "../model/model_manager.h"
#include "../response/response_dispatcher.h"

class GenieService;

class ChatRequestHandler
{
public:
    explicit ChatRequestHandler(GenieService *srv);

    void HandleWelcome(const Request &req, Response &res);

    void ImageGenerate(const httplib::Request &req, httplib::Response &res);

    void ChatCompletions(const httplib::Request &req, httplib::Response &res);

    void FetchModelList(const httplib::Request &req, httplib::Response &res);

    void TextSplitter(const httplib::Request &req, httplib::Response &res);

    void FetchProfile(const httplib::Request &req, httplib::Response &res);

    void ModelStop(const httplib::Request &req, httplib::Response &res);

    void ClearMessage(const httplib::Request &req, httplib::Response &res);

    void ReloadMessage(const httplib::Request &req, httplib::Response &res);

    void FetchMessage(const httplib::Request &req, httplib::Response &res);

    void ContextSize(const httplib::Request &req, httplib::Response &res);

    void ServiceExit(const httplib::Request &req, httplib::Response &res);

    void FetchModelStatus(const httplib::Request &req, httplib::Response &res);

    void UnloadModel(const httplib::Request &req, httplib::Response &res);

private:
    ModelManager &model_manager;
    std::unique_ptr<ChatHistory> chatHistory;
    std::unique_ptr<ResponseDispatcher> dispatcherPtr_;
    GenieService *srv_;
};

#endif //CHAT_REQUEST_HANDLER_H
