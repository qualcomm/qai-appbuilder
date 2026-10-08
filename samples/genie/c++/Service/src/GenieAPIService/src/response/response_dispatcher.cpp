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
#include <chrono>

namespace
{
    // 判断 convertToolCallJson() 的返回结果是否仍是 "unknow" 兜底（即 Layer0 正则修复链 +
    // Layer1 本地确定性提取均未能采纳出合法工具调用）。convertToolCallJson() 的返回值恒为
    // wrapJsonInToolCall() 包裹后的 "<tool_call>\n{...}\n</tool_call>" 形式（成功/失败两种
    // 结果都一样带标签，见 response_tools.cpp 第 377 行的唯一 return 出口），因此这里必须先
    // 用 ResponseTools::extractJsonFromToolCall 剥掉标签再 json::parse——直接 parse 带标签的
    // 字符串必然抛异常，会让本函数对任何输入都误判为 true（哪怕是完全合法的 tool_call），
    // 曾在真机验证中实测复现（QNN/qwen3-8b-8480：模型第一次就生成了完全合法的
    // {"name":"write",...}，仍被误判为 malformed，触发了不必要的 Layer2 重试，且 Layer3 因
    // partial_recovery 为空而把这个本该成功的 tool_call 降级成了 finish_reason=length 纯文本）。
    // 解析异常视为 unknow（保守判定，与调用方现有死循环检测逻辑的 catch(...) 保持一致）。
    bool IsUnknowToolCallJson(const std::string &converted_json_str)
    {
        try
        {
            json obj = json::parse(ResponseTools::extractJsonFromToolCall(converted_json_str));
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
    // 注：旧版 consecutive_unknow_tool_calls_（ResponseDispatcher 实例成员，生命周期与
    // 单次 HTTP 请求绑定，无法跨请求可靠累积）已迁移为 Layer3 分级终态协议 + 会话+模型
    // 维度的 ToolCallCircuitBreakerStore（LRU+TTL 单例，见 chat_request_handler/
    // tool_call_circuit_breaker_store.h），此处不再需要任何每请求重置逻辑。
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

    // Layer2 内部隐形自纠正重试的历史一致性兜底（仅 Harmony 格式 + 有状态会话相关）：
    // 若本次请求触发了重试且最终回滚（见下方 Layer2 代码块），proc_（HarmonyProcessor）
    // 的内部消息缓冲区（m_analysisMessages/m_finalMessages/m_commentaryMessages/
    // m_isToolCall）已经被重试轮次的内容覆盖，不能再信任事后调用 GetMessageForHistory()
    // 的实时结果——必须改用重试发起前抢先快照的历史文案，否则持久化到 ChatHistory 的内容
    // 会与客户端实际收到的（回滚后的首次尝试）内容不一致。默认不启用快照（无重试/重试
    // 成功/非 Harmony 时，历史写入逻辑与改动前完全一致）。详见 response_dispatcher.md。
    bool layer2_retry_rolled_back = false;
    bool has_harmony_history_snapshot = false;
    std::string snapshot_harmony_history_msg;
    bool snapshot_harmony_is_tool_call = false;  // 仅用于回滚后的调试日志文案，不影响持久化内容

    // Layer -1 兜底：无 <tool_call> 标签时的裸 JSON hold-back 状态（仅 General + is_tool_
    // 场景使用）。核心状态机（think_closed/post_think_judged/bare_json_hold/
    // bare_json_hold_released/held_visible 字段含义见 ResponseTools::BareJsonHoldState
    // 声明处注释）已提取到 response_tools.h/.cpp 的 ApplyBareJsonHoldBack()，便于
    // response_tools_layer1_selftest.cpp 脱离本类模拟"逐 chunk 到达"场景做离线自测；
    // 这里只保留状态实例本身与不属于"纯状态机"的心跳计时器。生成结束后由
    // finalize_generation_result() 内的 ResponseTools::DetectBareToolCall() 统一对完整
    // response_buffer 重新判定，这里的 hold-back 只负责"生成过程中不要提前把疑似裸 JSON
    // 的内容发给客户端"，两者是分离的关注点。Layer2 内部重试会复用同一个 genie_callback
    // 闭包，必须随其它状态一起重置（见下方 Layer2 重试代码块），否则首次尝试的残留状态会
    // 污染重试轮次。详见 response_dispatcher.md「Layer -1 hold-back」一节。
    ResponseTools::BareJsonHoldState bare_json_state;
    auto last_hold_keepalive_at = std::chrono::steady_clock::now();

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

        // ── Layer -1 兜底：无 <tool_call> 标签时的裸 JSON hold-back ──────────────
        // 只在声明了 tools、且尚未匹配到 <tool_call> 标签、且是 General 格式时生效
        // （Harmony 有自己的 channel 协议，不在此处理；标签路径本身不需要这层）。状态机本身
        // 已提取到 ResponseTools::ApplyBareJsonHoldBack()（response_tools.cpp），这里只负责
        // 调用并处理心跳保活（心跳依赖 sink，不属于"纯状态机"逻辑，留在调用方）。命中 hold
        // 时的内容存入 bare_json_state.held_visible，由生成结束后的统一释放逻辑（见
        // SendResponse 主体 had_tool_call_attempt 之后）决定最终发给客户端还是被 Layer3
        // tool_calls 取代。
        if (is_tool_ && !isToolResponse && GetEffectivePromptType() == PromptType::General)
        {
            ResponseTools::ApplyBareJsonHoldBack(bare_json_state, response_buffer, message, chunk);

            if (bare_json_state.bare_json_hold)
            {
                // hold 期间定期发心跳（约每 5 秒一次），防止代理因长时间无数据超时断连——
                // prefill_heartbeat 只覆盖 prefill 阶段，这里覆盖的是 decode 阶段的 hold 窗口。
                auto now = std::chrono::steady_clock::now();
                if (std::chrono::duration_cast<std::chrono::seconds>(
                        now - last_hold_keepalive_at).count() >= 5)
                {
                    SendKeepAlive(sink);
                    last_hold_keepalive_at = now;
                }
            }
        }

        // 模式检测：根据模型输出内容发送对应的状态反馈（每种状态只发送一次）
        // 注：tool_call 相关状态事件（通用"Calling tool..."与细粒度工具名状态）已不再在
        // 此处（流式生成过程中、刚检测到 <tool_call> 标记时）立即发送——此时无法确定最终
        // 是否真的会产出一个合法工具调用：Layer0/1/2 可能全部失败，Layer3 情形B 会降级为
        // 纯文本，提前发送会让客户端先收到"正在调用工具"提示、最终却收到不一致的纯文本
        // 结果。改为推迟到 finalize_tool_call_layer3()（生成结束后，已确认 Layer0/1/2
        // 成功或已决定走 Layer3 情形A/B）统一发送且只发送一次，见该函数末尾。详见
        // response_dispatcher.md「流式状态事件时序修正」一节（含心跳保活覆盖性结论）。
        if (is_stream_ && sink)
        {
            // 检测代码块（独立判断，不受 tool_call 影响，没有"最终结果可能矛盾"的风险，
            // 维持立即发送）
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

                        // 注：此处不再立即发送 "tool_call" 状态事件——是否真的产出了合法
                        // 工具调用要等 Layer0/1/2/3 全部跑完才能确定，提前发送有"客户端收到
                        // 提示、最终却是纯文本"的矛盾体验风险。统一改为 finalize_tool_call_layer3()
                        // 在确认最终结果后发送且只发送一次，见该函数与下方 Layer2/3 代码块。
                    }
                }
            }
            // ── Layer -1 兜底：无 <tool_call> 标签时的裸 JSON 工具调用检测 ────────────
            // 背景：模型有时会省略 <tool_call> 标签，直接输出裸 JSON（如 {"name":"read",...}），
            // 有时前面还带着 <think>...</think> 思考前缀、简短前置说明文字，或用 ```json
            // 代码围栏包裹。原有检测（要求 response_buffer trim 后必须直接以 '{' 开头）
            // 漏掉了这几类真实存在的输出形态——漏检时 isToolResponse 永远保持 false，
            // Layer0/1/2/3 全部被跳过，最终 finish_reason="stop"，客户端误判任务已完成，
            // 这正是本层要修复的缺口。收窄条件加上 is_tool_（未声明 tools 时不应触发）。
            // 检测逻辑复用既有的 ExtractBalancedJsonCandidates/TryParseLayer1Candidate/
            // ExtractNameField/NormalizeAndMatchToolName（见 ResponseTools::DetectBareToolCall
            // 实现），不重复实现修复逻辑；命中后交由下方既有的 convertToolCallJson()->
            // Layer1/2/3 链路统一处理。
            else if (!isToolResponse && is_tool_ && GetEffectivePromptType() == PromptType::General)
            {
                std::string visible_text = extractFinalAnswer(response_buffer);
                std::string wrapped;
                if (ResponseTools::DetectBareToolCall(visible_text, wrapped))
                {
                    isToolResponse = true;
                    toolResponse = wrapped;
                    My_Log{My_Log::Level::kWarning}
                        << "[SendResponse] General format: detected bare JSON tool call (no <tool_call> tag). "
                        << "Preview: " << wrapped.substr(0, std::min(wrapped.size(), size_t(120))) << std::endl;

                    // 注：同上，不在此处立即发送状态事件，统一交给 finalize_tool_call_layer3()。
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

                        // P0 修复：proc_->Clean() 会清空 HarmonyProcessor 的内部消息缓冲区
                        // （m_analysisMessages/m_finalMessages/m_commentaryMessages/m_isToolCall），
                        // 第二次 Query() 会用重试轮次的内容重新填充它们。若重试最终失败触发下方
                        // 回滚，历史写入阶段对 Harmony 格式会调用 GetMessageForHistory() 读取
                        // "当前" proc_ 状态——那时已是被重试污染的状态，不是回滚后客户端实际收到
                        // 的首次尝试内容。因此必须在 Clean() 之前抢先快照"如果现在要写历史，应该
                        // 写什么"，供回滚分支使用；重试成功分支则不使用这份快照，正常调用
                        // GetMessageForHistory() 读取新的、正确的内容。
                        if (GetEffectivePromptType() == PromptType::Harmony)
                        {
                            auto *harmony_proc_before_retry = dynamic_cast<HarmonyProcessor *>(proc_);
                            if (harmony_proc_before_retry)
                            {
                                snapshot_harmony_history_msg = harmony_proc_before_retry->GetMessageForHistory();
                                snapshot_harmony_is_tool_call = harmony_proc_before_retry->IsToolCall();
                                has_harmony_history_snapshot = true;
                            }
                        }

                        // 显式重置直接影响"本次生成结果内容判定"的状态。注：status_tool_call_sent_
                        // 无需在此特别处理——Step3 起该状态事件已推迟到 finalize_tool_call_layer3()
                        // （在 Layer2 重试与回滚都结束之后才调用一次），流式生成/重试期间完全不发送
                        // 任何工具调用相关状态帧，因此这里不存在"重试期间需不需要保留已发送标记"
                        // 的问题（详见 response_dispatcher.md 的时序说明）。
                        response_buffer.clear();
                        toolResponse.clear();
                        isToolResponse = false;
                        proc_->Clean();
                        is_tool_ = scratch_is_tool;
                        // Layer -1 hold-back 状态同样要随 Layer2 重试一起重置——genie_callback
                        // 是同一个闭包被复用，首次尝试遗留的状态若不清零会污染重试轮次的判定
                        // （见该状态声明处注释）。
                        bare_json_state = ResponseTools::BareJsonHoldState();
                        last_hold_keepalive_at = std::chrono::steady_clock::now();

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
                        // 联动回滚：Harmony 格式下 proc_ 的内部状态已被重试污染，下方历史写入
                        // 阶段不能再信任 GetMessageForHistory() 的实时结果，须改用上面抢先快照的
                        // 历史文案（has_harmony_history_snapshot 为 false 时说明当时不是 Harmony
                        // 格式或 dynamic_cast 失败，下方会安全退回原有逐次调用逻辑）。
                        layer2_retry_rolled_back = true;
                        My_Log{My_Log::Level::kWarning}
                            << "[ToolCallRepair][Layer2] Retry did not help; falling back to existing unknow "
                               "forwarding (unchanged behavior)." << std::endl;
                    }
                }
            }
        }
        // ─────────────────────────────────────────────────────────────────────────

        // ── Layer3：分级组合终态协议 ────────────────────────────────────────────
        // Layer0（正则修复链）+ Layer1（本地确定性提取，内嵌于 convertToolCallJson）+
        // Layer2（服务端内部隐形重试，上方代码块）全部未能恢复出合法工具调用后的终态
        // 兜底，取代原有"直接原样转发 name=\"unknow\"，连续 N 次才发一条客户端未必认识
        // 的模糊错误提示"机制（旧版 consecutive_unknow_tool_calls_ 死循环检测已删除）：
        //   情形A（识别出工具名，即使必需参数不全）：发出一个指向该真实工具的 tool_call
        //   （function.name=识别出的真实工具名，function.arguments=Layer1 侧已尽力恢复
        //   的参数——缺失的必需参数已填充空字符串占位），交由客户端走它本就熟悉的"工具
        //   执行报错→模型重试"标准闭环，而不是发明客户端不认识的协议。
        //   情形B（完全无法识别出任何工具名，概率很低，前两层已过滤掉大部分情况）：降级
        //   为纯文本回答，finish_reason="length"（绝不使用"stop"，避免被误判为任务已正常
        //   完成——很多客户端已对 length 有特殊处理逻辑）。
        // 会话+模型维度熔断：Layer0/1/2 任一层成功恢复 → RecordSuccess() 清零连续计数；
        // 真正走到本终态 → RecordLayer3Trigger() 计数，连续达到
        // circuit_breaker.consecutive_layer3_threshold 后，下一轮同一会话+模型的请求会在
        // ModelInputBuilder::Build() 里被动态降级 system prompt（跳过工具声明注入），从
        // 根源上避免模型继续做徒劳的工具调用尝试（见 model_input_builder.h）。
        // 本终态替换逻辑本身不受 tool_call_repair.enabled 门控——与 Layer1（同样不受该
        // 开关门控，Step1 既有先例）一致，属于不可关闭的核心正确性保证，而非可选新特性；
        // enabled=false 只关闭 Layer2 重试与熔断降级 system prompt 这两个"主动新增行为"
        // 的生效，不能关闭"绝不原样转发裸 name=unknow"这一硬约束。
        auto finalize_tool_call_layer3 = [&](const char *log_suffix)
        {
            ToolCallFailureReason failure_reason = ToolCallFailureReason::kNone;
            json partial_recovery;
            toolResponse = ResponseTools::convertToolCallJson(toolResponse, &failure_reason, &partial_recovery);
            // Skill 自动纠偏：使用运行时 SKILL 映射（从客户端 <available_skills> XML 动态解析）
            {
                const auto skill_mappings = model_config_.GetRuntimeSkillMappings();
                const auto& opt_cfg = model_config_.GetPromptOptimizationConfig();
                toolResponse = ResponseTools::AutoCorrectSkillCall(
                    toolResponse, skill_mappings, opt_cfg.enable_skill_auto_correction);
            }
            My_Log{} << "ResponseDispatcher::SendResponse" << log_suffix << ": \n" << toolResponse << std::endl;

            // 推迟发送的 "tool_call" 状态事件：只在确认已产出有效结果（Layer0/1/2 成功
            // 或 Layer3 情形A）时发送一次；tool_name 传空字符串时回退为通用 "Calling tool..."。
            // Layer3 情形B（降级为纯文本）不调用本函数，因为它本质上不是一个工具调用。
            auto send_tool_call_status_once = [&](const std::string &tool_name)
            {
                if (!status_tool_call_sent_)
                {
                    status_tool_call_sent_ = true;
                    if (is_stream_ && sink)
                    {
                        auto name_status = GetToolCallStatusByName(tool_name);
                        if (!name_status.first.empty())
                        {
                            SendStatusUpdate(sink, name_status.first, name_status.second);
                        }
                        else
                        {
                            SendStatusUpdate(sink, "tool_call", "Calling tool...");
                        }
                    }
                }
            };

            // 会话+模型维度熔断 key：session_key（首条 user 消息内容哈希，跨同一会话的多轮
            // 请求保持稳定）+ model_name；任一信息不可用（如 DLL 模式）时 key 为空字符串，
            // ToolCallCircuitBreakerStore 对空 key 的全部操作均安全地不做任何事。
            std::string circuit_breaker_key;
            std::string repetition_key;
            if (instance_config_ && !retry_request_data_.is_null()
                && retry_request_data_.contains("messages") && retry_request_data_["messages"].is_array())
            {
                const std::string session_key = TaskMemoBuilder::ComputeFirstMsgKey(retry_request_data_["messages"]);
                circuit_breaker_key = ToolCallCircuitBreakerStore::MakeKey(session_key, instance_config_->get_model_name());
                repetition_key = ToolCallRepetitionStore::MakeKey(session_key, instance_config_->get_model_name());
            }
            const bool redundant_guard_enabled = model_config_.GetToolCallRepairConfig().redundant_call_guard.enabled;

            if (!IsUnknowToolCallJson(toolResponse))
            {
                // Layer0/1/2 某一层成功恢复出合法工具调用：打断"连续触发 Layer3"的判定序列。
                ToolCallCircuitBreakerStore::GetInstance().RecordSuccess(circuit_breaker_key);
                finishReason = "tool_calls";
                std::string tool_name;
                try
                {
                    json parsed = json::parse(ResponseTools::extractJsonFromToolCall(toolResponse));
                    if (parsed.contains("name") && parsed["name"].is_string())
                    {
                        tool_name = parsed["name"].get<std::string>();
                    }
                    if (redundant_guard_enabled && !tool_name.empty())
                    {
                        const std::string signature = tool_name + "|" + parsed.value("arguments", json::object()).dump();
                        ToolCallRepetitionStore::GetInstance().RecordCall(repetition_key, signature);
                    }
                }
                catch (...) {}
                send_tool_call_status_once(tool_name);
            }
            else
            {
                int consecutive = ToolCallCircuitBreakerStore::GetInstance().RecordLayer3Trigger(circuit_breaker_key);
                My_Log{My_Log::Level::kWarning}
                    << "[ToolCallRepair][Layer3] Layer0/1/2 all failed to recover a valid tool call"
                    << log_suffix << " (reason=" << ResponseTools::ToolCallFailureReasonToString(failure_reason)
                    << ", consecutive_layer3_triggers=" << consecutive << "). Selecting terminal protocol..." << std::endl;

                if (partial_recovery.contains("name") && partial_recovery["name"].is_string()
                    && !partial_recovery["name"].get<std::string>().empty())
                {
                    // 情形A：工具名已确定（即使必需参数不全），发出指向该真实工具的 tool_call。
                    json best_effort = json::object();
                    best_effort["name"] = partial_recovery["name"];
                    best_effort["arguments"] = partial_recovery.value("arguments", json::object());
                    toolResponse = ResponseTools::wrapJsonInToolCall(best_effort.dump());
                    finishReason = "tool_calls";
                    isToolResponse = true;
                    send_tool_call_status_once(partial_recovery["name"].get<std::string>());
                    if (redundant_guard_enabled)
                    {
                        const std::string signature = partial_recovery["name"].get<std::string>() + "|"
                            + best_effort["arguments"].dump();
                        ToolCallRepetitionStore::GetInstance().RecordCall(repetition_key, signature);
                    }
                    My_Log{My_Log::Level::kWarning}
                        << "[ToolCallRepair][Layer3] Case A: emitting real tool_call '"
                        << partial_recovery["name"].get<std::string>()
                        << "' with best-effort (possibly incomplete) arguments." << std::endl;
                }
                else
                {
                    // 情形B：完全无法识别出任何工具名，降级为纯文本；finish_reason 设为协议
                    // 原生的 "length"（绝不使用 "stop"），避免被误判为任务已正常完成。
                    finishReason = "length";
                    isToolResponse = false;
                    toolResponse.clear();
                    My_Log{My_Log::Level::kWarning}
                        << "[ToolCallRepair][Layer3] Case B: no recoverable tool name; downgrading to plain "
                           "text with finish_reason=\"length\" (never \"stop\")." << std::endl;
                }
            }
        };
        // ─────────────────────────────────────────────────────────────────────────

        // Layer3 判定必须在 ChatHistory 写入、以及 connection_broken 分支之前完成一次：
        // had_tool_call_attempt 捕获"本次生成是否曾经产生过一次工具调用尝试"这一 Layer2/3
        // 判定前的原始信号，用于决定后续是否进入"发真实 tool_call / 降级纯文本"专属发送
        // 分支（普通文本已在 genie_callback 内逐 token 实时发送过，不应重复发送）；调用后
        // isToolResponse/toolResponse/finishReason 变为 Layer3 最终裁决后的状态，供 ChatHistory
        // 写入与后续发送逻辑统一消费，不再各自使用裁决前的 response_buffer（P0 修复：此前
        // ChatHistory 写入发生在本调用之前，General/非 Harmony 分支会把 Layer3 情形B 降级前
        // 的原始畸形内容写入历史，与客户端实际收到的干净文本产生分歧）。详见
        // response_dispatcher.md「ChatHistory 一致性」一节。
        bool had_tool_call_attempt = isToolResponse;
        if (had_tool_call_attempt)
        {
            finalize_tool_call_layer3(connection_broken ? " (connection_broken)" : "");
        }

        // Layer -1 hold-back 释放：若生成过程中曾因为"</think> 之后的可见文本以 '{' 或
        // 代码围栏开头"而暂缓发送（见 genie_callback 内的 hold-back 逻辑），且最终判定
        // 这不是一次工具调用尝试（had_tool_call_attempt==false，即从未匹配到标签、也未被
        // 上方 DetectBareToolCall 命中），则把攒住的内容作为一次性 content 补发给客户端——
        // 这段内容从未走过 genie_callback 里的正常发送路径，若不在此补发会永久丢失。
        // had_tool_call_attempt==true 时不发送：这段内容本就从未作为 delta.content 发送过，
        // 天然不会与随后 Layer3 发出的 tool_calls 帧产生"文本+工具调用"的重复问题。
        // 发送本身不依赖 connection_broken（broken 时 write 静默失败，与其余"无论连接是否
        // 断开都执行"的收尾步骤一致，如下方历史写入/PrintProfile）。
        if (!had_tool_call_attempt && !bare_json_state.held_visible.empty() && is_stream_ && sink)
        {
            ResponseTools::post_stream_data(*sink, "data",
                ResponseTools::responseDataJson(bare_json_state.held_visible, "", true));
            bare_json_state.held_visible.clear();
        }

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
                    // P0 修复：Layer2 重试失败回滚时，proc_ 的内部状态已被重试轮次污染，
                    // 此处改用重试发起前抢先快照的历史文案，而不是重新调用
                    // GetMessageForHistory() 读到污染后的内容（详见上方 Layer2 重试代码块
                    // 里 snapshot_harmony_history_msg 的注释）。未触发重试/重试成功/
                    // 快照当时未能取到时，回退到原有的实时调用逻辑，行为与改动前一致。
                    std::string history_msg = (layer2_retry_rolled_back && has_harmony_history_snapshot)
                                                   ? snapshot_harmony_history_msg
                                                   : harmony_proc->GetMessageForHistory();

                    // 诊断日志（仅回滚场景）：证明"若不使用快照会写入什么"与"实际写入了什么"
                    // 是否真的存在差异——GetMessageForHistory() 是纯读取（无副作用），重复调用
                    // 用于对照安全。用于真机验证本次修复解决的具体分歧，不影响实际写入内容。
                    if (layer2_retry_rolled_back && has_harmony_history_snapshot)
                    {
                        const std::string would_be_corrupted = harmony_proc->GetMessageForHistory();
                        My_Log{My_Log::Level::kWarning}
                            << "[ToolCallRepair][Layer2][HistorySnapshot] rollback occurred; using pre-retry "
                               "snapshot for ChatHistory (len=" << history_msg.length()
                            << ") instead of live post-retry proc_ state (len=" << would_be_corrupted.length()
                            << "). Divergence=" << (would_be_corrupted != history_msg ? "YES" : "no (identical)")
                            << std::endl;
                    }

                    if (!history_msg.empty())
                    {
                        chatHistory.AddMessage("assistant", history_msg);
                        
                        // 详细日志：回滚场景下 IsToolCall() 同样读的是被污染的实时状态，
                        // 改用快照的布尔值保持日志文案与实际持久化内容一致（仅影响日志文案，
                        // 不影响上面已经写入 ChatHistory 的 history_msg 本身）。
                        const bool is_tool_call_for_log = (layer2_retry_rolled_back && has_harmony_history_snapshot)
                                                               ? snapshot_harmony_is_tool_call
                                                               : harmony_proc->IsToolCall();
                        if (is_tool_call_for_log)
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
                // 非 Harmony 格式：Layer3 情形B（曾尝试工具调用但最终判定完全无法识别工具名，
                // 降级为纯文本）时，客户端收到的是经 extractSanitizedFinalText() 清洗过的
                // 干净文本，此处必须使用同一份清洗后内容，否则 ChatHistory 会残留原始
                // <tool_call> 标签/JSON 碎片，与客户端实际看到的内容产生分歧（P0 修复，详见
                // response_dispatcher.md「ChatHistory 一致性」一节）。真实工具调用
                // （Layer0/1/2 成功或 Layer3 情形A）与从未涉及工具调用的普通对话保持原有
                // 逻辑不变，避免引入回归。
                std::string final_answer = (had_tool_call_attempt && !isToolResponse)
                                                ? extractSanitizedFinalText(response_buffer)
                                                : extractFinalAnswer(response_buffer);
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

        // 真实 token 计数：修复 responseDataJson() 的 usage 字段此前硬编码为全零的缺陷
        // （导致下游 QAIModelBuilder 把本地模型响应误判为"空 usage 块"，回退到不可靠的
        // tokenizer 估算）。复用三后端（GGUF/MNN/QNN）都已实现的 handle->HandleProfile()
        // （PrintProfile() 内部已调用过一次用于日志，这里是请求结束时的第二次独立调用，
        // 不在逐 token 发送的性能敏感路径上，开销可忽略）。字段缺失/解析失败时安全回退为 0，
        // 与修复前的全零行为一致，不引入新的异常路径。只在本次请求结束时计算一次，下方按
        // finish_reason 分支选用，中间流式 content chunk 不受影响（仍使用默认的 0）。
        json profile_for_usage = handle->HandleProfile();
        size_t real_prompt_tokens = (profile_for_usage.is_object()
                                      && profile_for_usage.contains("num_prompt_tokens")
                                      && profile_for_usage["num_prompt_tokens"].is_number())
            ? profile_for_usage["num_prompt_tokens"].get<size_t>() : 0;
        size_t real_completion_tokens = (profile_for_usage.is_object()
                                          && profile_for_usage.contains("num_generated_tokens")
                                          && profile_for_usage["num_generated_tokens"].is_number())
            ? profile_for_usage["num_generated_tokens"].get<size_t>() : 0;

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
                // 若有工具调用，先发送工具调用响应（finalize_tool_call_layer3() 已在历史写入
                // 之前调用过一次，此处直接消费其裁决结果，不再重复调用）。
                if (had_tool_call_attempt)
                {
                    if (isToolResponse)
                    {
                        // Layer0/1/2 恢复成功，或 Layer3 情形A：发出真实工具 tool_call。
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
                    else
                    {
                        // Layer3 情形B：降级为纯文本（连接已断开，写入大概率静默失败，
                        // 仍尝试发送以保持与正常路径一致的行为）。extractSanitizedFinalText()
                        // 保证发出的内容不含残留 <tool_call> 标签，见 response_dispatcher.md。
                        std::string content = extractSanitizedFinalText(response_buffer);
                        if (!content.empty())
                        {
                            ResponseTools::post_stream_data(*sink, "data",
                                ResponseTools::responseDataJson(content, "", true));
                        }
                    }
                }
                // 发送结束标记
                if (!overflow_truncated)
                {
                    ResponseTools::post_stream_data(*sink, "data",
                        ResponseTools::responseDataJson("", finishReason, true, "", real_prompt_tokens, real_completion_tokens));
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

        // 若曾尝试工具调用，发送 Layer3 裁决后的结果给客户端（finalize_tool_call_layer3()
        // 已在历史写入之前调用过一次，此处直接消费其结果，不再重复调用）。
        if (had_tool_call_attempt)
        {
            if (isToolResponse)
            {
                // Layer0/1/2 恢复成功，或 Layer3 情形A：发出真实工具 tool_call，交由客户端
                // 走它本就熟悉的"工具执行报错→模型重试"标准闭环。
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
            else if (is_stream_ && sink)
            {
                // Layer3 情形B：降级为纯文本；finishReason 已在 finalize_tool_call_layer3
                // 内设为 "length"（绝不是 "stop"）。extractSanitizedFinalText() 保证发给
                // 客户端的内容已清洗掉任何残留 <tool_call> 标签/JSON 碎片，详见
                // response_dispatcher.md。
                std::string content = extractSanitizedFinalText(response_buffer);
                if (!content.empty())
                {
                    ResponseTools::post_stream_data(*sink, "data",
                        ResponseTools::responseDataJson(content, "", true));
                }
            }
        }

        if (is_stream_)
        {
            if (!overflow_truncated)
            {
                ResponseTools::post_stream_data(*sink, "data",
                    ResponseTools::responseDataJson("", finishReason, true, "", real_prompt_tokens, real_completion_tokens));
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
            // 统一走 extractSanitizedFinalText()（而非裸 extractFinalAnswer()）：非流式路径
            // 无论 Case A/B 都共用这一出口，必须保证 Layer3 情形B 降级为纯文本时不会连带
            // 泄漏残留 <tool_call> 标签，详见 response_dispatcher.md。
            std::string content = extractSanitizedFinalText(response_buffer);
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
            auto data = ResponseTools::responseDataJson(content, finishReason, false, toolResponse,
                                                         real_prompt_tokens, real_completion_tokens);
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

std::string ResponseDispatcher::extractSanitizedFinalText(const std::string &output)
{
    // 见 response_dispatcher.h 处的声明注释：这是三处 Layer3 情形B/非流式通用 content
    // 提取的唯一净化入口，任何后续调用点都应复用本方法，不要再直接调用 extractFinalAnswer()
    // 后当作纯文本使用。
    return ResponseTools::remove_tool_call_content(extractFinalAnswer(output));
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
