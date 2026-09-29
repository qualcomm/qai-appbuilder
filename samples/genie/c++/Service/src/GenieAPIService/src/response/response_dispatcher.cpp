//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "response_dispatcher.h"
#include "../chat_request_handler/model_input_builder.h"
#include "response_tools.h"

#include "log.h"
#include "../processor/general.h"
#include "../processor/harmony.h"
#include "watermark_provider_host.h"
#include <regex>
#include <algorithm>

namespace
{
    // 判断 convertToolCallJson() 的返回结果是否仍是 "unknow" 兜底（即 Layer0 正则修复链 +
    // Layer1 本地确定性提取均未能采纳出合法工具调用）。convertToolCallJson() 的返回值恒为
    // 一个可直接 json::parse 的 JSON 字符串（不带 <tool_call> 标签），因此这里不需要再调用
    // ResponseTools::extractJsonFromToolCall。解析异常视为 unknow（保守判定，与调用方现有
    // 死循环检测逻辑的 catch(...) 保持一致）。
    bool IsUnknowToolCallJson(const std::string &converted_json_str)
    {
        try
        {
            json obj = json::parse(converted_json_str);
            return obj.contains("name") && obj["name"].is_string()
                   && obj["name"].get<std::string>() == "unknow";
        }
        catch (...)
        {
            return true;
        }
    }

    // 判断 failure_reason 是否命中 tool_call_repair.internal_retry.skip_reasons 配置
    // （字符串比较，取值口径见 ResponseTools::ToolCallFailureReasonToString()）。
    bool IsFailureReasonSkipped(ToolCallFailureReason reason, const std::vector<std::string> &skip_reasons)
    {
        const std::string reason_str = ResponseTools::ToolCallFailureReasonToString(reason);
        return std::find(skip_reasons.begin(), skip_reasons.end(), reason_str) != skip_reasons.end();
    }

    // 构造 Layer2 内部隐形自纠正重试用的 role=tool 错误消息文案。
    // 复用真实工具执行出错时的消息格式（role:"tool", content:"Error: ..."），而不是发明新的
    // meta 指令——模型本身已经在训练/系统提示词层面理解"工具报错后应该怎么重试"这一标准模式，
    // 复用比新协议更可靠（详见 response_dispatcher.md 的架构决策记录）。
    // 可用工具名列表动态取自 PromptOptimizer::GetKnownToolSignatures()（Step1 已提升为公共
    // 权威表），避免与之脱节。
    std::string BuildToolRepairErrorMessage(ToolCallFailureReason reason)
    {
        auto join_known_tool_names = []() -> std::string
        {
            std::string joined;
            for (const auto &kv : PromptOptimizer::GetKnownToolSignatures())
            {
                if (!joined.empty()) joined += ", ";
                joined += kv.first;
            }
            return joined;
        };

        switch (reason)
        {
            case ToolCallFailureReason::kUnknownToolName:
                return "Error: unknown tool name. Available tools: " + join_known_tool_names() +
                       ". Please retry with one of these exact tool names in a single valid JSON object.";
            case ToolCallFailureReason::kMissingRequiredArgs:
                return "Error: missing required argument(s) for the tool you tried to call. "
                       "Please retry with the exact tool name and ALL of its required arguments "
                       "included in a single valid JSON object.";
            case ToolCallFailureReason::kAmbiguousMultipleCalls:
                return "Error: your response contained multiple ambiguous tool call candidates. "
                       "Please output exactly one single valid JSON tool_call object.";
            case ToolCallFailureReason::kTruncated:
                return "Error: your tool call output appears to have been truncated. "
                       "Please retry with a shorter, complete, single valid JSON object.";
            case ToolCallFailureReason::kUnparseable:
            case ToolCallFailureReason::kNone:
            default:
                return "Error: invalid tool call format, could not parse as JSON. "
                       "Please retry with a single valid JSON object, no extra text.";
        }
    }
}

ResponseDispatcher::ResponseDispatcher(IModelConfig &model_mgr,
                                       ChatHistory &chatHistory,
                                       const ModelInstanceConfig *instance_config) :
        chatHistory(chatHistory),
        model_config_(model_mgr),
        instance_config_(instance_config)
{
    ResetProcessor();
}

void ResponseDispatcher::ResetProcessor()
{
    if (proc_)
    {
        delete proc_;
        proc_ = nullptr;
    }

    // 修复：多模型场景下优先使用 instance_config_（per-model 配置）的 prompt type，
    // 而非 model_config_（全局 IModelConfig）的 prompt type。
    // 在多模型场景下，model_config_.get_prompt_type() 返回的是最后加载的单模型的 prompt type，
    // 会导致所有请求使用同一个 Processor，破坏多模型路由的正确性。
    PromptType prompt_type = instance_config_
        ? instance_config_->get_prompt_type()
        : model_config_.get_prompt_type();

    switch (int(prompt_type))
    {
        case PromptType::Harmony:
            proc_ = new HarmonyProcessor{};
            break;
        default:
            proc_ = new GeneralProcessor{};
    }

    chatHistory.Clear();
}

void ResponseDispatcher::Prepare(ModelInput &model_input,
                                 bool is_tool,
                                 bool is_stream,
                                 const httplib::Request &req,
                                 bool is_dll_mode,
                                 const json *request_data_for_retry)
{
    if (is_dll_mode)
        this->req_ = nullptr;
    else
        this->req_ = &const_cast<httplib::Request &>(req);

    this->model_input_ = model_input;
    is_stream_ = is_stream;
    is_tool_ = is_tool;
    // Layer2 内部隐形自纠正重试：保存本次请求的原始数据快照（深拷贝）。
    // 调用方未提供时保持 null，Layer2 在 SendResponse() 中据此判断是否可用（自动跳过）。
    this->retry_request_data_ = request_data_for_retry ? *request_data_for_retry : json();
    proc_->Clean();
    // 每次新请求重置状态追踪标志，避免上一次推理的状态影响本次
    status_tool_call_sent_ = false;
    status_code_sent_ = false;
    tool_call_name_status_sent_ = false;
    tool_call_accumulator_.clear();
    // 修复2：每次新请求重置 unknow 工具调用连续计数器
    // 注意：此计数器跨请求累积（不在此处重置），仅在成功工具调用后归零，
    // 以便检测跨多轮请求的死循环。
    // 实际上，每次新的用户消息（非 tool_call turn）应重置计数器。
    // 通过检查 is_tool_ 来区分：is_tool_=false 表示新用户消息，重置计数器。
    if (!is_tool)
    {
        consecutive_unknow_tool_calls_ = 0;
    }
}

void ResponseDispatcher::SendStatusUpdate(httplib::DataSink *sink,
                                          const std::string &status,
                                          const std::string &message)
{
    if (!is_stream_ || !sink)
        return;
    ResponseTools::post_stream_data(*sink, "data",
        ResponseTools::statusDataJson(status, message));
    My_Log{} << "[Status] " << status << ": " << message << std::endl;
}

void ResponseDispatcher::SendKeepAlive(httplib::DataSink *sink)
{
    if (!is_stream_ || !sink)
        return;
    // 发送空 delta.content 帧：HTTP 层感知到数据流动防止代理超时，客户端不渲染任何内容。
    ResponseTools::post_stream_data(*sink, "data",
        ResponseTools::responseDataJson("", "", true));
    My_Log{My_Log::Level::kDebug} << "[KeepAlive] sent empty delta frame" << std::endl;
}

bool ResponseDispatcher::SendResponse(size_t, httplib::DataSink *sink, httplib::Response *res, bool suppress_end_on_overflow)
{
    // 修复：改为使用 GetEffectiveHandle()（优先取每模型独立的 instance_config_ 句柄），
    // 而非全局 model_config_.get_genie_model_handle()（会在 ModelManager::LoadModel()
    // 加载任意新模型时被 Clean() 置空，导致所有模型的请求都可能拿到空句柄进而空指针崩溃）。
    auto handle = GetEffectiveHandle();
    if (!handle)
    {
        My_Log{My_Log::Level::kError}
            << "[SendResponse] Model handle is null (model may have been unloaded or reset). "
            << "Aborting request instead of crashing." << std::endl;
        constexpr char *null_handle_err =
            R"({"error": {"message": "Model handle unavailable, the model may have been unloaded or reset.", "type": "server_error", "code": 500}})";
        if (is_stream_ && sink)
        {
            ResponseTools::post_stream_data(*sink, "error", null_handle_err, false);
            ResponseTools::post_stream_data(*sink, "data", "[DONE]", true);
        }
        else if (res)
        {
            res->status = 500;
            res->set_content(null_handle_err, MIMETYPE_JSON);
        }
        return false;
    }
    std::string toolResponse; // Save tool call information
    std::string finishReason = "stop";
    response_buffer.clear();
    bool isToolResponse = false;


    bool connection_broken = false;  // Fix 3: track connection state across callback invocations

    auto genie_callback = [&](std::string &message)
    {
        // Fix 3: check connection before processing any message (including heartbeat keep-alive signals)
        if (!isConnectionAlive())
        {
            // Fix 4: 不在 callback 内部调用 handle->Stop()，避免死锁。
            // Stop() 会等待 impl_->done==true，而 done 只在 impl_->Query() 末尾设置，
            // 但 impl_->Query() 正在等待本 callback 返回 → 永久死锁，query_mutex_ 永不释放。
            // 正确做法：直接返回 false，触发 impl_->Query() 中的 should_stop=true → break，
            // Query() 自然退出并设置 done=true，query_mutex_ 正常释放。
            // Stop() 仅应从推理循环外部调用（如 /model/stop 接口）。
            connection_broken = true;
            return false;
        }

        // Heartbeat: empty message is a keep-alive signal from Query() — call SendKeepAlive()
        if (message.empty())
        {
            SendKeepAlive(sink);
            return true;  // connection is alive, no data to send
        }

#ifndef GENIEAPI_EXPORTS
        My_Log{}.original(true) << message;
#endif
        std::string chunk = message;

        auto result = preprocessStream(chunk, isToolResponse, toolResponse);
        isToolResponse = std::get<0>(result);
        // 修复：根据 Processor 类型正确使用 preprocessStream 的输出。
        //
        // preprocessStream 有两种输出机制，取决于 Processor 类型：
        //
        // ── GeneralProcessor（General 格式）──────────────────────────────────
        //   - chunkText（chunk）：内部工作变量，被 reinject 机制修改，不代表最终输出
        //   - 返回值第二元素（outputChunk）：经状态机过滤后的最终输出（可能为空）
        //   - 正确做法：始终使用 outputChunk（即使为空，表示该 token 不应发送）
        //   - 旧代码 bug：使用 chunk（原始 message），导致 '<tool_call>' 前缀泄漏
        //
        // ── HarmonyProcessor（Harmony 格式）─────────────────────────────────
        //   - chunkText（chunk）：被修改为 newContent（final 通道内容），是最终输出
        //   - 返回值第二元素：非工具调用时为 ""，工具调用时为 m_toolCallContent
        //   - 正确做法：使用 chunk（已被修改为 newContent）
        //   - 若使用 outputChunk，非工具调用时 chunk 变为 ""，内容丢失！
        //
        // 通过 GetEffectivePromptType() 区分两种格式，分别处理。
        if (GetEffectivePromptType() == PromptType::General)
        {
            // General 格式：始终使用 outputChunk（经状态机过滤的最终输出）
            // outputChunk 为空表示该 token 被状态机缓存（如 '<tool_call>' 前缀），不应发送
            chunk = std::get<1>(result);
        }
        // else Harmony 格式：保留 chunk（已被 HarmonyProcessor 修改为 newContent）

        response_buffer += message;  // Keep original message in buffer for history

        // 模式检测：根据模型输出内容发送对应的状态反馈（每种状态只发送一次）
        if (is_stream_ && sink)
        {
            // 检测 tool_call 开始标记，立即发送通用状态
            if (!status_tool_call_sent_ && message.find("<tool_call>") != std::string::npos)
            {
                SendStatusUpdate(sink, "tool_call", "Calling tool...");
                status_tool_call_sent_ = true;
                tool_call_accumulator_.clear();
                tool_call_name_status_sent_ = false;
            }

            // 若已检测到 tool_call 但尚未解析出工具名称，继续累积并尝试解析
            // 一旦解析出工具名称，发送细粒度状态（覆盖通用 tool_call 状态）
            if (status_tool_call_sent_ && !tool_call_name_status_sent_)
            {
                tool_call_accumulator_ += message;
                // 尝试从累积内容中提取 "name": "xxx"
                // Note: regex pattern is "name"\s*:\s*"([^"]+)"
                std::regex name_regex("\"name\"\\s*:\\s*\"([^\"]+)\"");
                std::smatch m;
                if (std::regex_search(tool_call_accumulator_, m, name_regex))
                {
                    std::string tool_name = m[1].str();
                    auto name_status = GetToolCallStatusByName(tool_name);
                    if (!name_status.first.empty())
                    {
                        // Send fine-grained status (e.g. "Executing script...", "Executing command...", etc.)
                        SendStatusUpdate(sink, name_status.first, name_status.second);
                        My_Log{} << "[Status] Tool name resolved: " << tool_name
                                 << " -> " << name_status.first << std::endl;
                    }
                    tool_call_name_status_sent_ = true;  // 无论是否有细粒度状态，都标记为已处理
                }
            }

            // 检测代码块（独立判断，不受 tool_call 影响）
            if (!status_code_sent_ && message.find("```") != std::string::npos)
            {
                SendStatusUpdate(sink, "writing_code", "Writing code...");
                status_code_sent_ = true;
            }
        }

        // 流式输出过滤：chunk 已在上方被替换为 preprocessStream 返回的 outputChunk，
        // 即经过状态机过滤后的最终输出内容：
        //   - 普通文本：outputChunk = 过滤后的文本（不含 <tool_call> 标签及其内容）
        //   - 工具调用期间：outputChunk = ""（空字符串，不发送任何内容）
        //   - <tool_call> 标签前缀（如 '<'、'tool'）：outputChunk = ""（等待后续匹配）
        
        bool content_sent = false;

        // 输出过滤后的内容（chunk）给客户端
        // chunk 在 preprocessStream() 调用后已被修改为 newContent（final 通道内容）
        // 工具调用内容已经被 preprocessStream 过滤掉
        if (!chunk.empty())
        {
            // 在工具调用场景下，进一步检查是否应该发送
            if (is_tool_ && isToolResponse)
            {
                // 工具调用场景：不发送任何内容，等待工具调用完成
                // 工具调用信息将在回调外部统一处理（lines 120-139）
                My_Log{}.original(true) << "\n";
                My_Log{} << "[Stream] Tool call detected, suppressing output" << std::endl;
                content_sent = true;
            }
            else
            {
                // 普通场景：发送 final 通道内容
                // preprocessStream 已经确保 chunk 只包含 final 通道内容
                if (is_stream_ && sink)
                {
                    ResponseTools::post_stream_data(*sink, "data",
                        ResponseTools::responseDataJson(chunk, "", true));
                }
                content_sent = true;
            }
        }

        // 工具调用期间，不需要额外处理
        // 工具调用完成后的处理在回调外部进行（lines 120-139）
        if (is_tool_ && isToolResponse && !GetEffectiveIsOutputAllText())
            return true;

        // 注意：这里的 is_stream_ 分支保持原有逻辑（向后兼容）
        // 为了避免与上方 chunk 重复发送，使用 content_sent 保护
        // chunk 在 preprocessStream() 调用后已经是过滤后的 final 通道内容，
        // 不会包含残缺的工具调用标签（preprocessStream 内部已处理）。
        if (is_stream_ && sink && !content_sent && !chunk.empty())
            ResponseTools::post_stream_data(*sink, "data",
                ResponseTools::responseDataJson(chunk, "", true));
        return true;
    };

    try
    {
        My_Log{}.original(true) << "\n";

        // 推理开始前发送状态反馈，让客户端知道模型正在工作
        SendStatusUpdate(sink, "inference", "Inferencing...");

        // ── Prefill 阶段心跳回调 ──────────────────────────────────────────────
        // 在 prefill（prompt 处理）期间，模型可能需要数十秒才能输出第一个 token。
        // 此期间 genie_callback 不会被调用，客户端或中间代理可能因长时间无数据而超时断开。
        // prefill_heartbeat 每隔 5 秒被 llama_cpp 后端调用一次：
        //   - 若连接正常：调用 SendKeepAlive() 向客户端发送保活消息，保持连接活跃
        //   - 若连接断开：返回 false，通知后端立即中止 prefill，节省算力
        // 注意：仅流式模式（is_stream_=true）且 sink 非空时才有意义；
        //       非流式模式（如安全检查）不需要心跳，传 nullptr 即可。
        ContextBase::PrefillHeartbeatCallback prefill_heartbeat = nullptr;
        if (is_stream_ && sink)
        {
            prefill_heartbeat = [&]() -> bool
            {
                if (!isConnectionAlive())
                {
                    My_Log{My_Log::Level::kWarning}
                        << "[SendResponse] Prefill heartbeat: connection broken, aborting prefill.\n";
                    // 设置 connection_broken，使 handle->Query() 返回 false 后
                    // 走连接断开路径（而非 500 错误路径），与 token 生成阶段断开的处理保持一致。
                    connection_broken = true;
                    return false; // 连接断开，通知后端中止 prefill
                }
                SendKeepAlive(sink);
                return true;
            };
        }
        // ── 心跳回调构造结束 ─────────────────────────────────────────────────

        My_Log{} << "--- Query Context Start ---" << std::endl;
        if (!handle->Query(model_input_, genie_callback, prefill_heartbeat))
        {
            // [任务4]: 返回详细的错误并且发送终止标记 [DONE]，防止客户端无限等待
            constexpr char *err = R"({"error": {"message": "Model query unavailable or generation failed internally.", "type": "server_error", "code": 500}})";
            if (is_stream_)
            {
                if (!connection_broken && isConnectionAlive()) 
                {
                    ResponseTools::post_stream_data(*sink, "error", err, false);
                    ResponseTools::post_stream_data(*sink, "data", "[DONE]", true);
                }
            }
            else
            {
                res->status = 500;
                res->set_content(err, MIMETYPE_JSON);
            }
            My_Log{} << "--- Query Context Failed ---\n" << std::endl;
            return false;
        }
        My_Log{}.original(true) << "\n";
        My_Log{} << "--- Query Context End ---\n" << std::endl;

        // Layer2 内部隐形自纠正重试（下方紧接的代码块）需要在重试的第二次 Query() 结束后，
        // 对同一套"finish_reason 判定 + Harmony/General 兜底工具调用检测"逻辑再跑一遍，
        // 因此提取为闭包以便原样复用，避免维护两份容易漂移的检测逻辑（首次生成结束后立即
        // 调用一次，Layer2 重试成功完成 Query() 后再调用一次）。
        auto finalize_generation_result = [&]()
        {
            // Fix 5: determine finish_reason based on how generation ended
            if (handle->was_stopped_by_output_limit())
            {
                finishReason = "length";
                My_Log{My_Log::Level::kWarning}
                    << "[SendResponse] Generation stopped due to output token limit. "
                    << "finish_reason = \"length\"" << std::endl;
            }

            // 查询结束后，强制完成工具调用处理
            // 这对于没有输出结束标记的情况很重要
            if (GetEffectivePromptType() == PromptType::Harmony && proc_)
            {
                auto* harmony_proc = dynamic_cast<HarmonyProcessor*>(proc_);
                if (harmony_proc)
                {
                    // ── 方案B：强制 flush final 通道残留内容 ──────────────────────────
                    // 背景：当 params_.special=false 时，<|return|> 以空字符串 "" 传给 callback，
                    // 走 heartbeat 分支，processChunk() 从未被调用，状态机停留在 IN_MESSAGE 状态，
                    // pendingBuffer 中积累的最后一段 final 内容无法被 flush。
                    // 即使方案A（params_.special=true）已修复根本原因，此处作为防御性兜底，
                    // 确保在任何情况下 final 通道内容都能完整输出。
                    std::string flushed_final = harmony_proc->FinalizeFinalChannel();
                    if (!flushed_final.empty())
                    {
                        if (ResponseTools::log_inference_stream)
                        {
                            My_Log{My_Log::Level::kInfo}
                                << "[SendResponse] FinalizeFinalChannel flushed " << flushed_final.length()
                                << " bytes of pending final content. "
                                << "Preview: \"" << flushed_final.substr(0, std::min(flushed_final.length(), size_t(80))) << "\""
                                << std::endl;
                        }
                        if (is_stream_ && sink)
                        {
                            ResponseTools::post_stream_data(*sink, "data",
                                ResponseTools::responseDataJson(flushed_final, "", true));
                            if (ResponseTools::log_inference_stream)
                            {
                                My_Log{My_Log::Level::kInfo}
                                    << "[SendResponse] Flushed final content sent to client via SSE." << std::endl;
                            }
                        }
                    }
                    else
                    {
                        if (ResponseTools::log_inference_stream)
                        {
                            My_Log{My_Log::Level::kInfo}
                                << "[SendResponse] FinalizeFinalChannel: no pending final content to flush "
                                << "(normal case when <|return|> was properly received)." << std::endl;
                        }
                    }
                    // ─────────────────────────────────────────────────────────────────

                    harmony_proc->FinalizeToolCall();

                    // 检查是否有工具调用
                    if (harmony_proc->IsToolCall())
                    {
                        isToolResponse = true;
                        toolResponse = harmony_proc->GetToolCallContent();

                        // 兜底：Harmony 格式下 <tool_call> 标记可能在流式输出中被过滤，
                        // 导致 genie_callback 中未能检测到，此处在 Query() 结束后补发状态。
                        // 注意：此时推理已完成，状态仅作为"已完成工具调用"的通知，
                        // 不影响后续的工具调用响应发送。
                        if (!status_tool_call_sent_)
                        {
                            SendStatusUpdate(sink, "tool_call", "Calling tool...");
                            status_tool_call_sent_ = true;
                            My_Log{} << "[Status] tool_call status sent (Harmony FinalizeToolCall fallback)" << std::endl;
                        }
                    }
                }
            }
            // ── General 格式兜底：检测裸 JSON 工具调用（无 <tool_call> 标签）──────────
            // 背景：模型有时会省略 <tool_call> 标签，直接输出裸 JSON（如 {"name":"read",...}）。
            // 流式处理时，genie_callback 逐 token 调用，preprocessStream() 状态机无法识别
            // 以 '{' 开头的裸 JSON 为工具调用，导致 isToolResponse 始终为 false。
            // 此处在 Query() 结束后，对 response_buffer 做一次整体检测：
            //   - 若 isToolResponse 仍为 false（流式处理未识别）
            //   - 且 response_buffer 以 '{' 开头（去除首尾空白后）
            //   - 且包含 "name" 字段（工具调用的必要字段）
            //   - 且不含 "</tool_call>"（避免重复处理已正常识别的工具调用）
            // 则将 response_buffer 视为裸 JSON 工具调用，补全标签后设置 isToolResponse=true。
            else if (!isToolResponse && GetEffectivePromptType() == PromptType::General)
            {
                // 去除首尾空白后检查
                std::string trimmed = response_buffer;
                size_t trim_start = trimmed.find_first_not_of(" \t\r\n");
                size_t trim_end   = trimmed.find_last_not_of(" \t\r\n");
                if (trim_start != std::string::npos)
                    trimmed = trimmed.substr(trim_start, trim_end - trim_start + 1);

                if (!trimmed.empty() && trimmed[0] == '{' &&
                    trimmed.find("\"name\"") != std::string::npos &&
                    trimmed.find("</tool_call>") == std::string::npos)
                {
                    // 找到最后一个 '}' 作为 JSON 结束位置，补全标签对
                    size_t last_brace = trimmed.rfind('}');
                    if (last_brace != std::string::npos)
                    {
                        std::string wrapped = "<tool_call>" + trimmed.substr(0, last_brace + 1) + "</tool_call>";
                        isToolResponse = true;
                        toolResponse   = wrapped;
                        My_Log{My_Log::Level::kWarning}
                            << "[SendResponse] General format: detected bare JSON tool call in response_buffer. "
                            << "Auto-wrapped with <tool_call>...</tool_call>. "
                            << "Preview: " << trimmed.substr(0, std::min(trimmed.size(), size_t(80))) << std::endl;

                        if (!status_tool_call_sent_)
                        {
                            SendStatusUpdate(sink, "tool_call", "Calling tool...");
                            status_tool_call_sent_ = true;
                        }
                    }
                }
            }
        };
        // ─────────────────────────────────────────────────────────────────────────
        finalize_generation_result();

        // ── Layer2：服务端内部隐形自纠正重试 ────────────────────────────────────
        // 触发条件：本次生成产生了一次工具调用尝试（isToolResponse=true），且经 Layer0
        // （既有正则修复链）+ Layer1（本地确定性提取，已内嵌于 convertToolCallJson）预览
        // 判定后仍会落回 name="unknow"，且失败原因未被 skip_reasons 排除（默认仅排除
        // kTruncated：token 预算耗尽导致的截断，结构信息已丢失，重试大概率再次超预算，
        // 属于"重试无意义"的失败原因）。此处仅做"预览判定"，不采纳其返回值——真正被客户端
        // 采纳的转换仍发生在下方既有的 connection_broken/isToolResponse 分支里对
        // convertToolCallJson 的调用（对那部分代码零改动，因为 convertToolCallJson 是纯
        // 函数，对同一输入重复调用结果一致、无副作用、无推理开销）。
        //
        // 若判定值得重试：构造一个与共享 chatHistory 完全隔离的 scratch ChatHistory +
        // ModelInputBuilder，用 Prepare() 时保存的原始请求消息快照（retry_request_data_，
        // 与 ModelInputBuilder::Build() 实际消费的 data 完全一致，未经 PreFilter/压缩）追加
        // 一条 assistant 原始畸形输出 + 一条 role=tool 错误消息，重新走完整 Build() 预算/
        // 压缩流水线得到新的 ModelInput，绝不裸文本拼接绕过压缩流水线。scratch 对象是函数
        // 局部变量，SendResponse() 返回后立即销毁，因此重试期间的首次畸形输出与纠错消息绝
        // 不会写入 ChatHistory，也绝不会发送给客户端任何中间态。
        if (isToolResponse && instance_config_ && !retry_request_data_.is_null()
            && retry_request_data_.contains("messages") && retry_request_data_["messages"].is_array())
        {
            const auto &repair_cfg = model_config_.GetToolCallRepairConfig();
            if (repair_cfg.enabled && repair_cfg.internal_retry.max_attempts > 0)
            {
                ToolCallFailureReason preview_reason = ToolCallFailureReason::kNone;
                const std::string preview_converted = ResponseTools::convertToolCallJson(toolResponse, &preview_reason);

                if (IsUnknowToolCallJson(preview_converted)
                    && !IsFailureReasonSkipped(preview_reason, repair_cfg.internal_retry.skip_reasons))
                {
                    My_Log{My_Log::Level::kWarning}
                        << "[ToolCallRepair][Layer2] Malformed tool call detected (reason="
                        << ResponseTools::ToolCallFailureReasonToString(preview_reason)
                        << "). Attempting 1 internal invisible retry (server-side, not visible to client)." << std::endl;

                    // 回滚快照：若重试本身出错（Query 失败/异常）或重试后仍是 unknow，
                    // "维持现状转发 unknow"——恢复到发起重试之前的首次生成结果，而不是让
                    // 重试半途产生的（同样有问题的）中间状态泄漏进最终响应。
                    // 注意：不快照 connection_broken——它反映的是底层连接的真实状态，
                    // 一旦在重试期间探测为断开，无论首次尝试连接是否正常都应当保留该事实。
                    const std::string snapshot_response_buffer = response_buffer;
                    const std::string snapshot_tool_response = toolResponse;
                    const std::string snapshot_finish_reason = finishReason;

                    bool retry_yielded_valid_tool_call = false;
                    try
                    {
                        json scratch_data = retry_request_data_;
                        json assistant_msg;
                        assistant_msg["role"] = "assistant";
                        assistant_msg["content"] = response_buffer;
                        json tool_error_msg;
                        tool_error_msg["role"] = "tool";
                        tool_error_msg["content"] = BuildToolRepairErrorMessage(preview_reason);
                        scratch_data["messages"].push_back(assistant_msg);
                        scratch_data["messages"].push_back(tool_error_msg);

                        // scratch ChatHistory/ModelInputBuilder：函数局部对象，与共享的
                        // chatHistory 完全隔离，SendResponse() 返回后自动销毁。
                        auto *mutable_instance_config = const_cast<ModelInstanceConfig *>(instance_config_);
                        ChatHistory scratch_chat_history(*mutable_instance_config);
                        ModelInputBuilder scratch_builder(scratch_chat_history, mutable_instance_config);

                        bool scratch_is_tool = false;
                        std::function<bool()> scratch_is_alive_fn = nullptr;
                        if (is_stream_ && sink)
                        {
                            scratch_is_alive_fn = [sink]() -> bool { return sink->is_writable(); };
                        }
                        ModelInput scratch_model_input =
                            scratch_builder.Build(scratch_data, scratch_is_tool, scratch_is_alive_fn);

                        // 显式重置直接影响"本次生成结果内容判定"的状态。注意：故意不重置
                        // status_tool_call_sent_/tool_call_name_status_sent_/
                        // tool_call_accumulator_——这三者只影响"是否需要向客户端发送状态
                        // 事件"，首次尝试已经发送过 tool_call 状态，重试对客户端完全隐形，
                        // 若照常重置会在同一逻辑请求内产生第二次几乎相同的 tool_call 状态帧
                        // （详见 response_dispatcher.md 的时序说明）。
                        response_buffer.clear();
                        toolResponse.clear();
                        isToolResponse = false;
                        proc_->Clean();
                        is_tool_ = scratch_is_tool;

                        My_Log{} << "--- Query Context Start (Layer2 internal retry) ---" << std::endl;
                        bool retry_query_ok = handle->Query(scratch_model_input, genie_callback, prefill_heartbeat);
                        My_Log{}.original(true) << "\n";
                        My_Log{} << "--- Query Context End (Layer2 internal retry) ---\n" << std::endl;

                        if (retry_query_ok && !connection_broken)
                        {
                            finalize_generation_result();

                            if (isToolResponse)
                            {
                                ToolCallFailureReason retry_reason = ToolCallFailureReason::kNone;
                                const std::string retry_converted =
                                    ResponseTools::convertToolCallJson(toolResponse, &retry_reason);
                                retry_yielded_valid_tool_call = !IsUnknowToolCallJson(retry_converted);
                                if (!retry_yielded_valid_tool_call)
                                {
                                    My_Log{My_Log::Level::kWarning}
                                        << "[ToolCallRepair][Layer2] Internal retry still malformed (reason="
                                        << ResponseTools::ToolCallFailureReasonToString(retry_reason)
                                        << "). Model likely did not understand the correction hint." << std::endl;
                                }
                            }
                        }
                        else
                        {
                            My_Log{My_Log::Level::kWarning}
                                << "[ToolCallRepair][Layer2] Internal retry Query() failed or connection broke."
                                << std::endl;
                        }
                    }
                    catch (const std::exception &e)
                    {
                        My_Log{My_Log::Level::kError}
                            << "[ToolCallRepair][Layer2] Exception during internal retry: " << e.what() << std::endl;
                        retry_yielded_valid_tool_call = false;
                    }

                    if (retry_yielded_valid_tool_call)
                    {
                        My_Log{} << "[ToolCallRepair][Layer2] Internal retry succeeded, "
                                    "replacing malformed first attempt with corrected result (invisible to client)."
                                 << std::endl;
                    }
                    else
                    {
                        // 维持现状：回滚到重试之前的首次生成结果，继续走既有的 unknow
                        // 转发路径（Layer3 落地后会把这个"最终失败出口"替换为分级终态协议）。
                        response_buffer = snapshot_response_buffer;
                        toolResponse = snapshot_tool_response;
                        isToolResponse = true;
                        finishReason = snapshot_finish_reason;
                        My_Log{My_Log::Level::kWarning}
                            << "[ToolCallRepair][Layer2] Retry did not help; falling back to existing unknow "
                               "forwarding (unchanged behavior)." << std::endl;
                    }
                }
            }
        }
        // ─────────────────────────────────────────────────────────────────────────

        // ========== P1 修复：统一历史消息存储格式 ==========
        // 历史消息管理
        // numResponse == -1 语义说明：
        // - 客户端将在每次请求中发送完整的对话历史
        // - 服务端不需要维护本地历史
        // - 这种模式下，服务端只负责处理当前请求，不保存状态
        //
        // Fix: 历史存储在 connection_broken 检查之前执行。
        // 历史存储是服务端内部状态管理，与客户端连接状态无关。
        // 即使客户端在生成完成后主动断开连接，服务端仍应保存历史，
        // 以确保下次请求时上下文完整（当 numResponse != -1 时）。
        if (!GetEffectiveStatelessMode())
        {
            // 正常模式：服务端维护历史
            if (GetEffectivePromptType() == PromptType::Harmony && proc_)
            {
                // ========== P1 修复：Harmony 格式历史消息处理 ==========
                // 根据 CoT 管理规则处理：
                // - 工具调用：保留 analysis + commentary + final 通道
                // - 一般对话：只保留 final 通道
                auto* harmony_proc = dynamic_cast<HarmonyProcessor*>(proc_);
                if (harmony_proc)
                {
                    std::string history_msg = harmony_proc->GetMessageForHistory();
                    
                    if (!history_msg.empty())
                    {
                        chatHistory.AddMessage("assistant", history_msg);
                        
                        // 详细日志
                        if (harmony_proc->IsToolCall())
                        {
                            My_Log{My_Log::Level::kDebug} << "[History] ✓ Added tool-related assistant message (full CoT preserved)" << std::endl;
                            My_Log{My_Log::Level::kDebug} << "[History]   - Includes: analysis + commentary + final channels" << std::endl;
                        }
                        else
                        {
                            My_Log{My_Log::Level::kDebug} << "[History] ✓ Added regular assistant message (final channel only)" << std::endl;
                            My_Log{My_Log::Level::kDebug} << "[History]   - CoT (analysis) discarded per Harmony spec" << std::endl;
                        }
                        
                        My_Log{My_Log::Level::kDebug} << "[History]   - Message length: " << history_msg.length() << " bytes" << std::endl;
                    }
                    else
                    {
                        My_Log{My_Log::Level::kInfo}
                            << "[History] ⚠ Empty history message from Harmony processor" << std::endl;
                    }
                }
                else
                {
                    My_Log{My_Log::Level::kError}
                        << "[History] ✗ Failed to cast processor to HarmonyProcessor" << std::endl;
                    // 降级处理：使用原始缓冲区
                    chatHistory.AddMessage("assistant", extractFinalAnswer(response_buffer));
                }
            }
            else
            {
                // 非 Harmony 格式使用原有逻辑
                std::string final_answer = extractFinalAnswer(response_buffer);
                chatHistory.AddMessage("assistant", final_answer);
                
                My_Log{My_Log::Level::kDebug} << "[History] ✓ Added assistant message (standard format)" << std::endl;
            }
        }
        else
        {
            My_Log{My_Log::Level::kDebug} << "[History] Skipped (numResponse == -1, client manages history)" << std::endl;
        }

        // Fix: PrintProfile 在 connection_broken 检查之前执行。
        // 性能统计是服务端内部监控，不依赖客户端连接，应始终执行。
        PrintProfile();

        // Fix: connection_broken 检查移到历史存储和性能统计之后。
        // 历史存储和性能统计是服务端内部状态，不依赖客户端连接，应在连接断开时仍然执行。
        // 即使连接断开，也尝试发送工具调用响应和 [DONE]，让客户端知道当前流已结束，
        // 可以继续下一轮交互。如果连接真的断开，write 会静默失败，不会造成额外问题。
        // 背景：在多轮工具调用场景中，模型推理完成后连接可能因网络抖动或代理超时而断开，
        // 若跳过 [DONE]，客户端将永久等待流结束信号，无法继续下一轮工具调用。
        // suppress_end_on_overflow=true 且输出因 token 上限截断时，
        // 跳过 [DONE]，由调用方（流式回退逻辑）负责发送云端响应或补发结束标记
        bool overflow_truncated = suppress_end_on_overflow && handle->was_stopped_by_output_limit();
        if (connection_broken)
        {
            My_Log{My_Log::Level::kWarning}
                << "[SendResponse] Connection was broken during generation. "
                << "History and profile stored. Attempting to send end-of-stream markers anyway." << std::endl;

            if (is_stream_ && sink)
            {
                // 若有工具调用，先发送工具调用响应
                if (isToolResponse)
                {
                    toolResponse = ResponseTools::convertToolCallJson(toolResponse);
                    // Skill 自动纠偏：使用运行时 SKILL 映射（从客户端 <available_skills> XML 动态解析）
                    // GetRuntimeSkillMappings() 返回本次请求中解析到的 name->path 映射，
                    // 用于将模型错误的 SKILL 直接调用改写为 read(SKILL.md) 调用
                    {
                        const auto skill_mappings = model_config_.GetRuntimeSkillMappings();
                        const auto& opt_cfg = model_config_.GetPromptOptimizationConfig();
                        toolResponse = ResponseTools::AutoCorrectSkillCall(
                            toolResponse, skill_mappings, opt_cfg.enable_skill_auto_correction);
                    }
                    My_Log{} << "ResponseDispatcher::sendStreamResponse (connection_broken): \n" << toolResponse << std::endl;

                    finishReason = "tool_calls";
                    std::string content;
                    if (!GetEffectiveIsOutputAllText())
                    {
                        content = ResponseTools::remove_tool_call_content(toolResponse);
                    }
                    if (!content.empty())
                    {
                        content += "\n\n";
                    }
                    std::string response_data = ResponseTools::responseDataJson(content, "", true, toolResponse);
                    My_Log{} << "[Tool Call Response] Sending to client (connection_broken): " << response_data << std::endl;
                    ResponseTools::post_stream_data(*sink, "data", response_data);
                }
                // 发送结束标记
                if (!overflow_truncated)
                {
                    ResponseTools::post_stream_data(*sink, "data", ResponseTools::responseDataJson("", finishReason, true));
                    ResponseTools::post_stream_data(*sink, "data", "[DONE]", true);
                    My_Log{My_Log::Level::kWarning}
                        << "[SendResponse] End-of-stream markers sent (connection_broken path)." << std::endl;
                }
                else
                {
                    My_Log{My_Log::Level::kWarning}
                        << "[SendResponse] suppress_end_on_overflow=true, skipping [DONE] (connection_broken path). "
                        << "Caller will handle cloud fallback or send end markers." << std::endl;
                }
            }
            return false;
        }

        // If there is a tool call, return the processed characters to the client.
        if (isToolResponse)
        {
            toolResponse = ResponseTools::convertToolCallJson(toolResponse);
            // Skill 自动纠偏：使用运行时 SKILL 映射（从客户端 <available_skills> XML 动态解析）
            {
                const auto skill_mappings = model_config_.GetRuntimeSkillMappings();
                const auto& opt_cfg = model_config_.GetPromptOptimizationConfig();
                toolResponse = ResponseTools::AutoCorrectSkillCall(
                    toolResponse, skill_mappings, opt_cfg.enable_skill_auto_correction);
            }
            My_Log{} << "ResponseDispatcher::sendStreamResponse: \n" << toolResponse << std::endl;

            // ── 修复2+3：unknow 工具调用死循环检测 ──────────────────────────────
            // 当 convertToolCallJson 无法解析工具调用时，会将工具名设为 "unknow"。
            // 这通常是因为模型生成的 JSON 中包含字面控制字符（如换行符）。
            // 若连续出现 kMaxConsecutiveUnknowToolCalls 次，说明陷入死循环：
            //   - 模型不断生成相同的无效工具调用
            //   - 客户端无法执行，返回错误
            //   - 模型再次生成相同调用
            // 解决方案：向客户端返回一个特殊的 tool_response，明确告知模型
            // "工具调用格式错误，请直接回答用户"，引导模型退出循环。
            {
                // 从 toolResponse 中提取工具名（去除 <tool_call> 标签后解析）
                std::string tool_json_str = ResponseTools::extractJsonFromToolCall(toolResponse);
                // 去除首尾空白
                size_t ts = tool_json_str.find_first_not_of(" \t\r\n");
                size_t te = tool_json_str.find_last_not_of(" \t\r\n");
                if (ts != std::string::npos)
                    tool_json_str = tool_json_str.substr(ts, te - ts + 1);

                bool is_unknow_tool = false;
                try
                {
                    json tool_obj = json::parse(tool_json_str);
                    if (tool_obj.contains("name") && tool_obj["name"].is_string())
                    {
                        is_unknow_tool = (tool_obj["name"].get<std::string>() == "unknow");
                    }
                }
                catch (...) {}

                if (is_unknow_tool)
                {
                    ++consecutive_unknow_tool_calls_;
                    My_Log{My_Log::Level::kWarning}
                        << "[UnknowToolLoop] Detected unknow tool call #" << consecutive_unknow_tool_calls_
                        << " (max=" << kMaxConsecutiveUnknowToolCalls << ")" << std::endl;

                    if (consecutive_unknow_tool_calls_ >= kMaxConsecutiveUnknowToolCalls)
                    {
                        // 修复3：超过阈值，向客户端发送错误 tool_response，引导模型退出循环
                        My_Log{My_Log::Level::kWarning}
                            << "[UnknowToolLoop] Consecutive unknow tool calls exceeded limit ("
                            << kMaxConsecutiveUnknowToolCalls << "). "
                            << "Sending error tool_response to break the loop." << std::endl;

                        // 构造错误 tool_response：告知模型工具调用格式错误，请直接回答
                        // 使用 finish_reason="tool_calls" 但附带错误信息，
                        // 让客户端将此错误作为 tool_response 返回给模型
                        json error_tool_response = {
                            {"status", "error"},
                            {"tool", "unknow"},
                            {"error", "Tool call format error: the tool call JSON contains unescaped control characters (e.g. literal newlines in string values). Please do NOT retry the tool call. Instead, directly answer the user's question based on what you already know."}
                        };
                        // 重置计数器，避免下一轮继续触发
                        consecutive_unknow_tool_calls_ = 0;

                        // 将错误信息作为 tool_response 发送给客户端
                        // 客户端会将此作为 tool 角色消息返回给模型，引导模型退出循环
                        std::string error_tool_call_json = "<tool_call>\n" + error_tool_response.dump() + "\n</tool_call>";
                        finishReason = "tool_calls";
                        if (is_stream_)
                        {
                            std::string response_data = ResponseTools::responseDataJson("", "", true, error_tool_call_json);
                            My_Log{} << "[UnknowToolLoop] Sending error tool_response to client: " << response_data << std::endl;
                            ResponseTools::post_stream_data(*sink, "data", response_data);
                        }
                        // 跳过正常的工具调用发送流程
                        goto send_finish_reason;
                    }
                }
                else
                {
                    // 成功的工具调用：重置连续计数器
                    if (consecutive_unknow_tool_calls_ > 0)
                    {
                        My_Log{} << "[UnknowToolLoop] Successful tool call, resetting consecutive_unknow counter." << std::endl;
                        consecutive_unknow_tool_calls_ = 0;
                    }
                }
            }
            // ─────────────────────────────────────────────────────────────────────

            finishReason = "tool_calls";
            std::string content;

            if (!GetEffectiveIsOutputAllText())
            {
                content = ResponseTools::remove_tool_call_content(toolResponse);
            }
            if (!content.empty())
            {
                content += "\n\n";
            }

            if (is_stream_)
            {
                std::string response_data = ResponseTools::responseDataJson(content, "", true, toolResponse);
                My_Log{} << "[Tool Call Response] Sending to client: " << response_data << std::endl;
                ResponseTools::post_stream_data(*sink, "data", response_data);
            }
        }

        send_finish_reason:

        if (is_stream_)
        {
            if (!overflow_truncated)
            {
                ResponseTools::post_stream_data(*sink, "data", ResponseTools::responseDataJson("", finishReason, true));
                ResponseTools::post_stream_data(*sink, "data", "[DONE]", true);
            }
            else
            {
                My_Log{My_Log::Level::kWarning}
                    << "[SendResponse] suppress_end_on_overflow=true, skipping [DONE]. "
                    << "Caller will handle cloud fallback or send end markers." << std::endl;
            }
        }
        else
        {
            std::string content = extractFinalAnswer(response_buffer);
            if (WatermarkProviderHost::Instance().HasTextHook())
            {
                const auto* vt = WatermarkProviderHost::Instance().GetVTable();
                char* watermarked = vt->text_hook_apply(content.c_str());
                if (watermarked)
                {
                    content = watermarked;
                    vt->text_hook_free_string(watermarked);
                }
            }
            auto data = ResponseTools::responseDataJson(content, finishReason, false, toolResponse);
            res->set_content(data, MIMETYPE_JSON);
        }
        return true;
    }
    catch (const std::exception &e)
    {
        My_Log{My_Log::Level::kError} << "raise the exception while processing stream response: \n"
                                      << e.what() << "\n";
                                      
        if (!is_stream_) {
            res->status = 500;
            res->set_content(R"({"error": {"message": "Internal server error", "type": "server_error", "code": 500}})", MIMETYPE_JSON);
            return false;
        }

        // [任务4]: 发送标准的错误信息并终止流，防止客户端陷入等待
        if (!req_->is_connection_closed())
        {
            json error_json;
            if (dynamic_cast<const ReportError *>(&e))
            {
                error_json = {{"error", {{"message", e.what()}, {"type", "report_error"}, {"code", 400}}}};
            }
            else
            {
                error_json = {{"error", {{"message", std::string("Model generation error: ") + e.what()}, {"type", "server_error"}, {"code", 500}}}};
            }
            ResponseTools::post_stream_data(*sink, "error", error_json.dump(), false);
            ResponseTools::post_stream_data(*sink, "data", "[DONE]", true);
        }
        return false;
    }
}

bool ResponseDispatcher::isConnectionAlive() const
{
    if (!req_)
        return true;

    auto closed = req_->is_connection_closed();
    if (closed)
    {
        // http_busy_ 全局标志已在多模型重构中移除（参见 Multi_Model_Refactoring_Implementation_Guide.md §4.2）
        // 不再需要重置该标志，连接状态由 ContextBase::query_mutex_ 细粒度锁管理
        My_Log{My_Log::Level::kError} << "Client connection has been broken (Client or Proxy disconnected proactively)\n" << std::endl;
    }
    return !closed;
}

void ResponseDispatcher::PrintProfile()
{
    My_Log{} << "--- Token Summary Start ---" << std::endl;
    // 修复：使用 GetEffectiveHandle()（优先取每模型独立的 instance_config_ 句柄），而非
    // 全局 model_config_.get_genie_model_handle()——后者在多模型场景下会被 ModelManager::Clean()
    // 置空，此处原代码在未做任何空指针检查的情况下直接调用 ->HandleProfile()，是另一处会导致
    // 进程崩溃的空指针解引用缺陷，现补上空值防御。
    auto handle = GetEffectiveHandle();
    if (!handle)
    {
        My_Log{My_Log::Level::kWarning}
            << "[PrintProfile] Model handle is null, skip profile summary." << std::endl;
        My_Log{} << "--- Token Summary End ---\n";
        return;
    }
    auto json_str = handle->HandleProfile();
    if (json_str.empty())
    {
        goto done;
    }

    try
    {
        My_Log{} << "Time to First Token: "
                 << std::fixed
                 << std::setprecision(2)
                 << json_str.at("time_to_first_token").get<std::string>()
                 << " s" << std::endl;

        My_Log{} << "Token Generation Time: "
                 << std::fixed
                 << std::setprecision(2)
                 << json_str.at("token_generation_time").get<std::string>()
                 << " s" << std::endl;

        My_Log{} << "Num Prompt Tokens: "
                 << json_str.at("num_prompt_tokens")
                 << ", Text Length: " << model_input_.text_.length()
                 << std::endl;

        My_Log{} << "Prompt Processing Rate: "
                 << std::fixed
                 << std::setprecision(2)
                 << json_str.at("prompt_processing_rate").get<std::string>()
                 << " toks/sec" << std::endl;

        My_Log{} << "Num Generated Tokens: "
                 << json_str.at("num_generated_tokens")
                 << ", Text Length: " << response_buffer.length()
                 << std::endl;

        My_Log{} << "Token Generation Rate: "
                 << std::fixed
                 << std::setprecision(2)
                 << json_str.at("token_generation_rate").get<std::string>()
                 << " toks/sec" << std::endl;
    }
    catch (std::exception &e)
    {
        My_Log{My_Log::Level::kError} << "profile print failed:" << e.what() << std::endl;
    }

    done:
    My_Log{} << "--- Token Summary End ---\n";
}

ResponseDispatcher::~ResponseDispatcher()
{
    if (proc_)
    {
        delete proc_;
        proc_ = nullptr;
    }
}

std::string ResponseDispatcher::extractFinalAnswer(const std::string &output)
{
    // 检查是否是 Harmony 格式（优先使用 instance_config_ 的 prompt type）
    if (GetEffectivePromptType() == PromptType::Harmony)
    {
        // Harmony 格式：只提取 final 通道的内容
        if (proc_)
        {
            return dynamic_cast<HarmonyProcessor*>(proc_)->GetFinalContent();
        }
        return output;
    }
    else
    {
        // 原有逻辑：提取 </think> 之后的内容
        const std::string tag = "</think>";
        size_t pos = output.find(tag);
        if (pos != std::string::npos)
        {
            // Extract the content after the </think>.
            return output.substr(pos + tag.length());
        }
        else
        {
            // If the <think> tag is not in the result, return the original string.
            return output;
        }
    }
}

std::string ResponseDispatcher::getCompleteMessageForHistory(const std::string &output)
{
    if (GetEffectivePromptType() == PromptType::Harmony)
    {
        // Harmony 格式：获取完整的格式化消息
        if (proc_)
        {
            return dynamic_cast<HarmonyProcessor*>(proc_)->GetCompleteMessage();
        }
        return output;
    }
    else
    {
        return output;
    }
}

// ============================================================
// GetToolCallStatusByName：根据工具名称返回细粒度状态标识和可读消息
//
// 匹配规则（子字符串匹配，大小写敏感）：
//   脚本执行类：execute_script / run_script / exec_script
//   命令执行类：execute_command / run_command / exec_command / shell / bash
//   文件操作类：read_file / write_file / create_file / delete_file
//   搜索/网络类：search / browse / fetch
//   代码生成类：write_code / generate_code
//
// 返回 {"", ""} 表示无细粒度状态，调用方使用已发送的通用 "tool_call" 状态即可。
// ============================================================
std::pair<std::string, std::string> ResponseDispatcher::GetToolCallStatusByName(const std::string &tool_name)
{
    // Script execution
    if (tool_name.find("execute_script") != std::string::npos ||
        tool_name.find("run_script")     != std::string::npos ||
        tool_name.find("exec_script")    != std::string::npos)
    {
        return {"executing_script", "Executing script..."};
    }

    // Command execution
    if (tool_name.find("execute_command") != std::string::npos ||
        tool_name.find("run_command")     != std::string::npos ||
        tool_name.find("exec_command")    != std::string::npos ||
        tool_name.find("shell")           != std::string::npos ||
        tool_name.find("bash")            != std::string::npos)
    {
        return {"executing_command", "Executing command..."};
    }

    // File operations
    if (tool_name.find("read_file")   != std::string::npos ||
        tool_name.find("write_file")  != std::string::npos ||
        tool_name.find("create_file") != std::string::npos ||
        tool_name.find("delete_file") != std::string::npos)
    {
        return {"file_operation", "Operating on file..."};
    }

    // Search / network
    if (tool_name.find("search") != std::string::npos ||
        tool_name.find("browse") != std::string::npos ||
        tool_name.find("fetch")  != std::string::npos)
    {
        return {"searching", "Searching..."};
    }

    // Code generation
    if (tool_name.find("write_code")    != std::string::npos ||
        tool_name.find("generate_code") != std::string::npos)
    {
        return {"writing_code", "Writing code..."};
    }

    // 无细粒度状态：返回空字符串，调用方使用通用 tool_call 状态
    return {"", ""};
}
