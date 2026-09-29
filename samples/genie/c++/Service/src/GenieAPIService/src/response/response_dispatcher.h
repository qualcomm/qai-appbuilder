//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef RESPONSE_DISPATCHER_H
#define RESPONSE_DISPATCHER_H

#include <httplib.h>

#include <nlohmann/json.hpp>

using json = nlohmann::ordered_json;

#include "../processor/processor.h"
#include "../processor/harmony.h"
#include "../model/def.h"
#include "../context/context_base.h"
#include "../model/model_config.h"
#include "../model/model_instance_config.h"

class ChatHistory;

class ModelInput;

class ModelProcessor;

class ResponseDispatcher
{
public:
    // 多模型模式：接受 ModelInstanceConfig（per-model 配置）以正确选择 Processor
    ResponseDispatcher(IModelConfig &model_mgr,
                       ChatHistory &chatHistory,
                       const ModelInstanceConfig *instance_config = nullptr);

    ~ResponseDispatcher();

    void ResetProcessor();

    void Prepare(ModelInput &model_input,
                 bool is_tool,
                 bool is_stream,
                 const httplib::Request &req,
                 bool is_dll_mode = false,
                 const json *request_data_for_retry = nullptr);

    bool SendResponse(size_t, httplib::DataSink *sink, httplib::Response *res, bool suppress_end_on_overflow = false);

    // 向流式客户端发送状态反馈事件（不含结束符）
    // 仅在 is_stream_=true 且 sink 非空时生效
    void SendStatusUpdate(httplib::DataSink *sink, const std::string &status, const std::string &message);

    // 向流式客户端发送保活消息，防止客户端或中间代理因长时间无数据而超时断开连接
    // 发送空 delta.content 帧：HTTP 层感知到数据流动防止代理超时，客户端不渲染任何内容
    void SendKeepAlive(httplib::DataSink *sink);

    // 根据工具名称返回对应的细粒度状态标识和可读消息
    // 返回 {"", ""} 表示无细粒度状态，使用通用 tool_call 状态即可
    // 支持的工具类型：脚本执行、命令执行、文件操作、搜索/网络、代码生成
    static std::pair<std::string, std::string> GetToolCallStatusByName(const std::string &tool_name);

    static inline std::string MIMETYPE_JSON = "application/json; charset=utf-8";

private:
    void PrintProfile();

    bool isConnectionAlive() const;

    std::string extractFinalAnswer(const std::string &output);

    // Layer3 情形B（纯文本兜底）与非流式通用 content 提取的统一安全净化入口。
    // extractFinalAnswer() 本身不知道"这段文本可能是一次被误判/未能恢复出合法工具名的
    // 畸形 <tool_call> 输出"，因此这里再叠加一次 ResponseTools::remove_tool_call_content()
    // 清洗，确保客户端永远不会看到残留的 <tool_call> 标签或裸 JSON 碎片。三处调用点
    // （流式 Case B / connection_broken Case B / 非流式通用出口）统一复用本方法，
    // 避免各自维护、后续再次漂移；设计动机与真机证据见 response_dispatcher.md。
    // 定义放在 .cpp（而非就地 inline）：本方法体依赖 ResponseTools 的完整声明，而本头文件
    // 不包含 response_tools.h（部分 TU，如 GenieAPILibrary.cpp，只单独包含本头文件）。
    std::string extractSanitizedFinalText(const std::string &output);

    // 新增：获取完整消息用于历史存储
    std::string getCompleteMessageForHistory(const std::string &output);

    // 辅助方法：获取当前模型的 prompt type（优先使用 instance_config_）
    PromptType GetEffectivePromptType() const
    {
        return instance_config_ ? instance_config_->get_prompt_type() : model_config_.get_prompt_type();
    }

    // 辅助方法：获取当前模型的 numResponse（优先使用 instance_config_）
    int GetEffectiveNumResponse() const
    {
        return instance_config_ ? instance_config_->getnumResponse() : model_config_.getnumResponse();
    }

    // 辅助方法：获取当前模型是否为无状态模式（numResponse == -1）。
    // 复用 GetEffectiveNumResponse() 已有的 instance/global 回退逻辑；权威定义见 ModelInstanceConfig::IsStatelessMode()。
    bool GetEffectiveStatelessMode() const
    {
        return GetEffectiveNumResponse() == -1;
    }

    // 辅助方法：获取当前模型的 isOutputAllText（优先使用 instance_config_）
    bool GetEffectiveIsOutputAllText() const
    {
        return instance_config_ ? instance_config_->getisOutputAllText() : model_config_.getisOutputAllText();
    }

    // 辅助方法：获取当前模型的推理句柄（优先使用 instance_config_，即每模型独立配置）。
    // 修复：多模型场景下 model_config_.get_genie_model_handle()（ModelManager 的全局单模型句柄）
    // 会在 ModelManager::LoadModel() 加载任意新模型时被 Clean() 置空，导致所有模型的请求都可能
    // 拿到空句柄进而空指针解引用崩溃。instance_config_ 是构造时注入的每模型独立配置，
    // 不受 Clean() 影响，应优先使用；仅当 instance_config_ 为空（单模型模式）时才回退到全局句柄。
    std::shared_ptr<ContextBase> GetEffectiveHandle()
    {
        return instance_config_ ? const_cast<ModelInstanceConfig*>(instance_config_)->i_model_config_.get_genie_model_handle().lock()
                                 : model_config_.get_genie_model_handle().lock();
    }

    std::tuple<bool, std::string> preprocessStream(std::string &chunkText,
                                                   bool isToolResponse,
                                                   std::string &toolResponse)
    {
        return proc_->preprocessStream(chunkText, isToolResponse, toolResponse);
    }

    bool is_stream_{};
    bool is_tool_{};
    // 状态追踪：避免在同一次推理中重复发送相同的状态事件
    bool status_tool_call_sent_{false};
    bool status_code_sent_{false};
    // 死循环/系统性能力熔断计数器已迁移为会话+模型维度的进程内单例存储
    // ToolCallCircuitBreakerStore（chat_request_handler/tool_call_circuit_breaker_store.h，
    // LRU+TTL，模式对齐 TaskMemoStore）。旧版 consecutive_unknow_tool_calls_（
    // ResponseDispatcher 实例成员，生命周期与单次 HTTP 请求绑定，无法跨请求可靠累积）与
    // kMaxConsecutiveUnknowToolCalls 已随 Layer3 分级终态协议一起移除，见 response_dispatcher.md。
    ChatHistory &chatHistory;
    IModelConfig &model_config_;
    // 多模型模式：per-model 配置（优先于 model_config_ 的全局状态）
    // 若非空，ResetProcessor() 使用此配置的 get_prompt_type() 而非 model_config_.get_prompt_type()
    const ModelInstanceConfig *instance_config_{nullptr};
    std::string response_buffer; // The response buffer is used to store the response content of the model.
    httplib::Request *req_{};
    ModelProcessor *proc_{};
    ModelInput model_input_;
    // Layer2 内部隐形自纠正重试：Prepare() 时保存的原始请求数据快照（深拷贝，与
    // ModelInputBuilder::Build() 实际消费的 data 完全一致，未经 PreFilter/压缩)，用于
    // 重试时追加一条 assistant 原始输出 + 一条 role=tool 错误消息后重新走完整 Build()
    // 预算/压缩流水线构造 scratch ModelInput。默认 null（json 默认构造），表示调用方
    // 未提供（如 DLL 模式），此时 Layer2 自动跳过，不影响现有行为。
    json retry_request_data_;
};

#endif //RESPONSE_DISPATCHER_H
