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
#include <memory>

class GenieService;
class ModelManager;
class GenieRoutingGateway;

class ChatRequestHandler
{
public:
    explicit ChatRequestHandler(GenieService *srv);

    void HandleWelcome(const httplib::Request &req, httplib::Response &res);

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
    GenieService *srv_;

    // GenieRoutingGateway：内容安全检查 + 智能路由（按配置初始化）
    // 使用 shared_ptr 而非 unique_ptr，以便在流式响应 lambda 中安全捕获副本，
    // 避免 ChatRequestHandler 析构后 lambda 持有悬空指针导致未定义行为。
    std::shared_ptr<GenieRoutingGateway> genieRoutingGateway_;
    
    // 注意：移除了 chatHistory, dispatcherPtr_, input_builder_ 成员变量
    // 这些对象现在在 ChatCompletions 方法中作为局部变量创建，
    // 使 ChatRequestHandler 无状态，支持并发请求处理
};

#endif //CHAT_REQUEST_HANDLER_H
