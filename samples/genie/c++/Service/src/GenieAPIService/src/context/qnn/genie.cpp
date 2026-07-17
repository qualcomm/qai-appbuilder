//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "genie.h"
#include "genie_interface.h"
#include "utils.h"
#include "log.h"
#include "config_fixer.h"
#include <cstring>
#include <filesystem>
#include <system_error>

void GenieLog_Callback(const GenieLog_Handle_t  /*handle*/,
                       const char *fmt,
                       GenieLog_Level_t level,
                       uint64_t /*timestamp*/,
                       va_list args)
{
    auto length = std::vsnprintf(nullptr, 0, fmt, args);
    if (length < 0)
    {
        My_Log{My_Log::Level::kWarning} << "bad args and fmt in genie callback" << std::endl;
        return;
    }

    auto *buf = new char[length];
    My_Log::Level my_level;
    switch (level)
    {
        case GENIE_LOG_LEVEL_ERROR:
            my_level = My_Log::Level::kError;
            break;
        case GENIE_LOG_LEVEL_WARN:
            my_level = My_Log::Level::kWarning;
            break;
        case GENIE_LOG_LEVEL_INFO:
            my_level = My_Log::Level::kInfo;
            break;
        case GENIE_LOG_LEVEL_VERBOSE:
            my_level = My_Log::Level::kVerbose;
            break;
    }

    std::vsnprintf(buf, length, fmt, args);
    while (buf[length - 2] == '\n')
    {
        buf[length - 2] = '\0';
        length--;
    }
    delete[] buf;
}

void GenieContext::inference_thread()
{
    while (true)
    {
        std::unique_lock<std::mutex> lock(m_request_lock);
        m_request_cond.wait(lock,
                            [this] { return m_request_ready.load(); });     // m_request_ready == true, wakeup thread; m_request_ready == false, sleep continually.
        if (m_thread_exit)
        {
            return;
        }

        try
        {
            auto status = inf_impl_->inf_->GenieDialogQuery();
            if (GENIE_STATUS_SUCCESS != status && GENIE_STATUS_WARNING_ABORTED != status
                && GENIE_STATUS_WARNING_CONTEXT_EXCEEDED != status)
            {
                inference_succeed_ = false;
                My_Log{My_Log::Level::kError} << "get response from GenieDialog failed: " << status
                                              << " current token size: " << inf_impl_->inf_->cur_length_ << "\n";
            }
            else
            {
                inference_succeed_ = true;
                if (GENIE_STATUS_WARNING_CONTEXT_EXCEEDED == status)
                {
                    // 模型自身未能在真实上下文耗尽前主动收尾,这是模型输出质量层面的缺陷
                    // (而不是服务缺陷),SDK 已把这个状态归类为警告(非错误)。这里不再当作
                    // 请求失败(inference_succeed_ 保持 true,已生成内容按正常成功路径原样
                    // 返回给客户端),但仍以 error 级别打一条带 [MODEL_DEFECT] 标记的日志,
                    // 供 test_service.py 侧扫描服务端日志、归类为模型缺陷诊断信息。
                    stopped_by_output_limit_ = true;
                    My_Log{My_Log::Level::kError} << "[MODEL_DEFECT] reason=sdk_context_exceeded model="
                                                  << model_config_.get_model_name()
                                                  << " token_size=" << inf_impl_->inf_->cur_length_ << "\n";
                }
            }
        }
        catch (const std::exception &e)
        {
            inference_succeed_ = false;
            My_Log{My_Log::Level::kError} << "GenieDialogQuery threw exception: " << e.what() << std::endl;
        }
        catch (...)
        {
            inference_succeed_ = false;
            My_Log{My_Log::Level::kError} << "GenieDialogQuery threw unknown exception" << std::endl;
        }

        m_inference_busy = false;
        m_request_ready = false;
        m_stream_cond.notify_one();  // Notify that inference is complete
    }
}

bool GenieContext::Query(const ModelInput &model_input, const Callback &callback,
                         PrefillHeartbeatCallback /*prefill_heartbeat*/)
{
    // 注意：QNN/Genie 后端目前不支持 prefill 阶段心跳（Genie SDK 不提供批次级回调）。
    // prefill_heartbeat 参数保留以满足接口兼容性，暂不使用。

    // [修复] 序列化 Query() 调用：与 llama_cpp.cpp 保持一致，加 query_mutex_ 锁。
    // QNN/Genie 模型上下文不是线程安全的，安全检查、复杂度评估和主推理
    // 都通过同一个模型句柄调用 Query()，必须通过此锁保证互斥。
    // 缺少此锁会导致：
    //   1. 安全检查/复杂度检查完成后 KV cache 状态被污染
    //   2. 主推理调用时出现 "KV update count exceeds" 错误
    auto t_lock_start = std::chrono::steady_clock::now();
    std::lock_guard<std::mutex> lock(query_mutex_);
    auto t_lock_acquired = std::chrono::steady_clock::now();
    int64_t lock_wait_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
            t_lock_acquired - t_lock_start).count();
    if (lock_wait_ms > 10)
    {
        My_Log{My_Log::Level::kWarning}
                << "[QNN] query_mutex_ wait time: " << lock_wait_ms << "ms"
                << " (lock contention detected!)\n";
    }
    else
    {
        My_Log{} << "[QNN] query_mutex_ acquired immediately (wait=" << lock_wait_ms << "ms)\n";
    }

    const char *query_type = get_query_type_label(model_input);
    My_Log{} << "[QNN Restore] Query begin: type=" << query_type
             << ", model_name=" << model_config_.get_model_name()
             << ", model_dir=" << m_model_dir_ << std::endl;

    Genie_Status_t status = 0;
    if (!kv_path_.empty() && GENIE_STATUS_SUCCESS != (status = GenieDialog_restore(m_DialogHandle, kv_path_.c_str())))
    {
        throw std::runtime_error("restore kv failed: " + std::to_string(status));
    }

    stopped_by_output_limit_ = false;
    inf_impl_->inf_->cur_length_ = TokenLength(model_input.text_);
    if (!inf_impl_->inf_->set_content(const_cast<ModelInput &>(model_input)))
    {
        return false;
    }

    m_request_ready = true;
    m_inference_busy = true;
    m_request_cond.notify_one();

    std::string response;
    bool callback_rejected = false;  // Fix 2: track whether callback refused further data
    auto last_output_time = std::chrono::steady_clock::now();  // Heartbeat: track last output time

    while (m_inference_busy)
    {
        {
            std::unique_lock<std::mutex> lock(m_stream_lock);
            // Wait for new data or inference completion (with timeout for heartbeat check)
            m_stream_cond.wait_for(lock, std::chrono::milliseconds(HEARTBEAT_INTERVAL_MS),
                                   [this] { return !m_stream_answer.empty() || !m_inference_busy; });

            if (!m_stream_answer.empty())
            {
                response = std::move(m_stream_answer);
                // m_stream_answer is now in a valid but unspecified (empty) state after move
                last_output_time = std::chrono::steady_clock::now();  // Heartbeat: reset timer on output
            }
        }

        if (response.empty())
        {
            // Heartbeat: if no output for HEARTBEAT_INTERVAL_MS, send a keep-alive message
            auto now = std::chrono::steady_clock::now();
            auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                    now - last_output_time).count();
            if (elapsed_ms >= HEARTBEAT_INTERVAL_MS)
            {
                std::string heartbeat = "";  // empty string signals a keep-alive tick to SendKeepAlive()
                if (!callback(heartbeat))
                {
                    My_Log{My_Log::Level::kWarning}
                            << "[Query] Heartbeat callback rejected, stopping generation." << std::endl;
                    callback_rejected = true;
                    Stop();
                    break;
                }
                last_output_time = now;  // reset timer after heartbeat
            }
            continue;
        }

        if (!callback(response))
        {
            callback_rejected = true;  // Fix 2: mark rejection
            // 连接断开时 genie_callback 已调用 handle->Stop()（发送 ABORT 信号）
            // 这里再调用一次 Stop() 作为保险，确保推理线程能尽快退出
            Stop();
            break;
        }

        response.clear();
    }

    // [修复] 竞态条件：callback 返回 false 触发 Stop()+break 后，
    // inference_thread_ 中的 GenieDialogQuery() 可能仍在处理 ABORT 信号，
    // m_inference_busy 尚未变为 false。
    // 若此时立即释放 query_mutex_，下一个 Query() 调用的 GenieDialog_reset()
    // 会与仍在运行的 GenieDialogQuery() 产生竞态，导致 SSD 模型状态混乱。
    // 修复：等待 inference_thread_ 真正完成（m_inference_busy 变为 false）后再继续。
    // 超时保护：最多等待 5 秒，防止 ABORT 信号未被响应时永久阻塞。
    if (callback_rejected && m_inference_busy)
    {
        My_Log{My_Log::Level::kInfo}
                << "[Query] Waiting for inference_thread_ to finish after Stop()..." << std::endl;
        const int kMaxWaitMs = 5000;
        // [性能优化] 使用 m_stream_cond 等待推理线程完成，避免 sleep_for 轮询延迟。
        // inference_thread 在设置 m_inference_busy=false 后会调用 m_stream_cond.notify_one()，
        // 因此这里可以立即被唤醒，而不是最多等待 10ms。
        std::unique_lock<std::mutex> wait_lock(m_stream_lock);
        bool finished = m_stream_cond.wait_for(wait_lock,
                                               std::chrono::milliseconds(kMaxWaitMs),
                                               [this] { return !m_inference_busy.load(); });
        if (!finished)
        {
            My_Log{My_Log::Level::kWarning}
                    << "[Query] inference_thread_ did not finish within " << kMaxWaitMs
                    << "ms after Stop(). Proceeding anyway." << std::endl;
        }
        else
        {
            My_Log{My_Log::Level::kInfo}
                    << "[Query] inference_thread_ finished (wait_for returned)." << std::endl;
        }
    }

    if (!inference_succeed_)
    {
        return false;
    }

    // Fix 2: only send remainder data if callback has not rejected
    if (!callback_rejected && !m_stream_answer.empty())
    {
        callback(m_stream_answer);
        m_stream_answer.clear();
    }
    else if (callback_rejected)
    {
        m_stream_answer.clear();  // discard buffered data when connection is broken
    }

    // [修复] SSD 模型 KV cache 污染问题：
    // 对于非主推理类型（安全检查、复杂度评估、脱敏、Phase-1 摘要等），推理完成后额外调用一次
    // GenieDialog_reset，确保 SSD（Speculative Decoding）的 forecast buffer 和
    // branch 状态被彻底清除，不会污染后续的主推理。
    // 背景：GenieDialog_reset 在每次 Query() 开始时调用，但对于 SSD 模型，
    // 安全检查推理结束后 SSD 内部状态（forecast tokens、branch 预测等）仍然残留，
    // 导致主推理续写安全检查的输出（如输出 "reason" 而非正确的工具调用）。
    // genie-t2t-run 不做安全检查，因此不存在此问题。
    //
    // SUMMARIZATION（Phase -1 摘要推理）同样需要 post-query reset：
    // 摘要推理使用与主推理相同的 GenieContext（同一 m_DialogHandle），
    // 若不 reset，摘要推理的 KV cache 状态会残留，影响后续摘要推理或主推理的正确性。
    if (std::string(query_type) != "MAINAGENT_INFERENCE" &&
        std::string(query_type) != "SUBAGENT_INFERENCE")
    {
        My_Log{My_Log::Level::kInfo}
                << "[QNN] Post-query reset for non-main-inference type=" << query_type
                << " (clearing SSD forecast state)" << std::endl;
        if (GENIE_STATUS_SUCCESS != GenieDialog_reset(m_DialogHandle))
        {
            My_Log{My_Log::Level::kWarning}
                    << "[QNN] Post-query GenieDialog_reset failed for type=" << query_type << std::endl;
            // 非致命错误，继续返回 true（推理本身已成功）
        }
    }

    return true;
}

GenieContext::GenieContext(const ModelInstanceConfig &model_config) :
        ContextBase(model_config)
{
    Genie_Status_t status = 0;
    auto fixer = ConfigFixer{model_config};

    if (GENIE_STATUS_SUCCESS != GenieDialogConfig_createFromJson(fixer.FixConfig().dump().c_str(), &m_ConfigHandle))
    {
        throw std::runtime_error("Failed to create the Genie Dialog config.");
    }

    status = GenieLog_create(nullptr, GenieLog_Callback, get_genie_log_level(), &m_LogHandle);
    if ((GENIE_STATUS_SUCCESS != status) || (!m_LogHandle))
    {
        throw std::runtime_error("Failed to create the Log handle.");
    }

    status = GenieDialogConfig_bindLogger(m_ConfigHandle, m_LogHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        throw std::runtime_error("Failed to bind the log handle with the dialog config");
    }

    status = GenieProfile_create(nullptr, &m_ProfileHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        throw std::runtime_error("Failed to create the profile handle");
    }

    status = GenieDialogConfig_bindProfiler(m_ConfigHandle, m_ProfileHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        throw std::runtime_error("Failed to bind the profile handle with the dialog config");
    }

    if (GENIE_STATUS_SUCCESS != GenieDialog_create(m_ConfigHandle, &m_DialogHandle))
    {
        throw std::runtime_error("create the genie dialog failed");
    }

    status = GenieSamplerConfig_createFromJson(fixer.FixSampler().dump().c_str(), &m_SamplerConfigHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        throw std::runtime_error("Failed to create sampler config");
    }

    status = GenieDialog_getSampler(m_DialogHandle, &m_SamplerHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        throw std::runtime_error("Failed to get sampler");
    }

    std::vector<std::string> store_paths;
    if (fixer.has_ssd_prefix_)
        goto ahead;

    store_paths = {
            model_config.get_model_path() + "/",
            model_config.get_model_path() + "/Prefix/"
    };

    for (auto &path: store_paths)
    {
        if (File::IsFileExist(path + "kv-cache.primary.qnn-htp"))
        {
            kv_path_ = path;
            My_Log{} << "kv_path: " << kv_path_ << std::endl;
            break;
        }
    }

    ahead:
    if (!inf_impl_)
    {
        inf_impl_ = new QInterfaceImpl{this};
    }

    if (!m_stream_thread)
    {
        m_stream_thread = std::make_unique<std::thread>(&GenieContext::inference_thread, this);
    }
}

GenieContext::~GenieContext()
{
    My_Log{} << "GenieContext::~GenieContext():\n";

    int32_t status = 0;

    // [修复] 析构顺序竞态：必须先 join 推理线程，确保线程完全退出后再释放任何 SDK 句柄。
    // 原始代码在 notify_one() 后立即释放 m_DialogHandle，而推理线程可能刚被唤醒正在执行
    // GenieDialogQuery()，访问已释放的 SDK 内部 mutex，导致
    // "exits with 2147483647, undefined m_mutex handle object" 崩溃。
    if (m_stream_thread)
    {
        m_thread_exit = true;
        m_request_ready = true;
        m_request_cond.notify_one();

        // 先 join，等推理线程完全退出后再释放 SDK 句柄
        m_stream_thread->join();
        m_stream_thread = nullptr;
        m_request_ready = false;
        m_thread_exit = false;
    }

    // 推理线程已退出，现在安全地释放所有 SDK 句柄
    if (m_ConfigHandle != nullptr)
    {
        if (GENIE_STATUS_SUCCESS != GenieDialogConfig_free(m_ConfigHandle))
        {
            My_Log{} << "Failed to free the Genie Dialog config.\n";
        }
    }

    if (m_DialogHandle != nullptr)
    {
        if (GENIE_STATUS_SUCCESS != GenieDialog_free(m_DialogHandle))
        {
            My_Log{} << "Failed to free the Genie Dialog.\n";
        }
    }

    status = GenieSamplerConfig_free(m_SamplerConfigHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        My_Log{} << "Failed to free the sampler config." << std::endl;
    }

    status = GenieLog_free(m_LogHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        My_Log{} << "Failed to free the Log handle." << std::endl;
    }

    status = GenieProfile_free(m_ProfileHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        My_Log{} << "Failed to free the profile handle." << std::endl;
    }

    delete inf_impl_;
    inf_impl_ = nullptr;
    My_Log{} << "GenieContext::~GenieContext() Done:\n";
}

bool GenieContext::Stop()
{
    if (GENIE_STATUS_SUCCESS != GenieDialog_signal(m_DialogHandle, GENIE_DIALOG_ACTION_ABORT))
    {
        My_Log{} << "Failed to stop generation.\n";
        return false;
    }

    return true;
}

bool GenieContext::SetStopSequence(const std::string &stop_sequences)
{
    if (GENIE_STATUS_SUCCESS != GenieDialog_setStopSequence(m_DialogHandle, stop_sequences.c_str()))
    {
        My_Log{} << "Failed to set stop sequence.\n";
        return false;
    }

    return true;
}

bool GenieContext::GenerateTextToken(const std::string &text, const int32_t *&buf, uint32_t &len)
{
    // Early return for empty string: GenieTokenizer_encode returns -1 for empty input,
    // which would trigger a spurious "encode failed" error log. Return 0 tokens directly.
    if (strlen(text.c_str()) == 0)
    {
        buf = nullptr;
        len = 0;
        return true;
    }

    // [修复] UTF-8 安全防线：GenieTokenizer_encode 底层调用 Rust tokenizer，
    // 要求输入必须是合法的 UTF-8。若收到无效 UTF-8 字节序列，会在 Rust 侧 panic 崩溃。
    // 此处在调用 tokenizer 之前，先检测并修复无效 UTF-8 字节（替换为 '?'）。
    // 注意：这里使用局部副本，不修改调用方的原始字符串。
    std::string safe_text = text;
    bool has_invalid = sanitize_utf8_inplace(safe_text);
    if (has_invalid)
    {
        My_Log{My_Log::Level::kWarning}
                << "[GenerateTextToken] Detected and sanitized invalid UTF-8 bytes to prevent tokenizer crash."
                << " original_size=" << text.size() << std::endl;
    }
    const std::string &encode_text = has_invalid ? safe_text : text;

    GenieTokenizer_Handle_t tokenizerHandle = nullptr;
    Genie_Status_t status = GenieDialog_getTokenizer(m_DialogHandle, &tokenizerHandle);
    if (status != GENIE_STATUS_SUCCESS)
    {
        My_Log{}.original(true) << "\n";
        My_Log{My_Log::Level::kError} << "get tokenizer failed: " << status << std::endl;
        return false;
    }

    status = GenieTokenizer_encode(tokenizerHandle, encode_text.c_str(),
                                   [](const size_t size, const char **allocatedData)
                                   {
                                       *allocatedData = reinterpret_cast<const char *>(malloc(size));
                                   },
                                   &buf,
                                   &len);

    if (status != GENIE_STATUS_SUCCESS)
    {
        My_Log{}.original(true) << "\n";
        My_Log{My_Log::Level::kError} << "encode failed: " << status << ", "
                                      << "the string length is: " << text.size() << std::endl;
        return false;
    }
    return true;
}

size_t GenieContext::TokenLength(const std::string &text)
{
    const int32_t *buf;
    uint32_t len;
    if (!GenerateTextToken(text, buf, len))
    {
        return text.size();
    }

    free((void *) buf);
    return len;
}

void GenieContext::applyLora(const std::string &engineRole, const std::string &loraAdapterName)
{
    int32_t status = GenieDialog_applyLora(m_DialogHandle, engineRole.c_str(), loraAdapterName.c_str());
    if (GENIE_STATUS_SUCCESS != status)
    {
        throw std::runtime_error("Failed to apply the LoRA adapter.");
    }
}

void GenieContext::setLoraStrength(const std::string &engineRole,
                                   const std::unordered_map<std::string, float> &alphaValue)
{
    for (auto it = alphaValue.begin(); it != alphaValue.end(); it++)
    {
        int32_t status = GenieDialog_setLoraStrength(m_DialogHandle, engineRole.c_str(), it->first.c_str(), it->second);
        if (GENIE_STATUS_SUCCESS != status)
        {
            throw std::runtime_error("Failed to set the LoRA alpha strength.");
        }
    }
}

json GenieContext::HandleProfile()
{
    const Genie_AllocCallback_t callback([](size_t size, const char **data)
                                         {
                                             *data = (char *) malloc(size);
                                             if (*data == nullptr)
                                             {
                                                 My_Log{} << "cannot allocate memory for JSON data.\n";
                                             }
                                         });

    const char *jsonData = nullptr;
    const Genie_Status_t status = GenieProfile_getJsonData(m_ProfileHandle, callback, &jsonData);
    if (GENIE_STATUS_SUCCESS != status)
    {
        My_Log{My_Log::Level::kError} << "get the profile data failed: " << status << "\n";
        return "";
    }

    std::string jsonStr(jsonData);
    free((char *) jsonData);

    json result;
    try
    {
        json j = json::parse(jsonStr);
        if (!j["components"].empty() && j["components"][0]["type"] == "dialog")
        {
            const auto &events = j["components"][0]["events"];
            if (!events.empty())
            {
                const auto &last_event = events.back();
                if (last_event.at("type") == "GenieDialog_query")
                {
                    std::ostringstream oss;

                    // time_to_first_token (Genie SDK value unit: μs, convert to s)
                    oss << std::fixed << std::setprecision(2)
                        << last_event.at("time-to-first-token")["value"].get<double>() / 1000000.0;
                    result["time_to_first_token"] = oss.str();

                    // token_generation_time (Genie SDK value unit: μs, convert to s)
                    oss.str("");
                    oss << std::fixed << std::setprecision(2)
                        << last_event.at("token-generation-time")["value"].get<double>() / 1000000.0;
                    result["token_generation_time"] = oss.str();

                    // prompt_processing_rate
                    oss.str("");
                    oss << std::fixed << std::setprecision(2)
                        << last_event.at("prompt-processing-rate")["value"].get<double>();
                    result["prompt_processing_rate"] = oss.str();

                    // token_generation_rate
                    oss.str("");
                    oss << std::fixed << std::setprecision(2)
                        << last_event.at("token-generation-rate")["value"].get<double>();
                    result["token_generation_rate"] = oss.str();

                    // integer values
                    result["num_prompt_tokens"] = last_event.at("num-prompt-tokens")["value"];
                    result["num_generated_tokens"] = last_event.at("num-generated-tokens")["value"];
                }
            }
        }
    }
    catch (std::exception &e)
    {
        My_Log{My_Log::Level::kError} << "parse profile failed: " << jsonStr << "\n";
        return "";
    }
    return result;
}

GenieLog_Level_t GenieContext::get_genie_log_level()
{
    switch (My_Log::Level_)
    {
        case My_Log::Level::kError:
            return GENIE_LOG_LEVEL_ERROR;
        case My_Log::Level::kWarning:
        case My_Log::Level::kInfo:
            return GENIE_LOG_LEVEL_INFO;
        default:
            return GENIE_LOG_LEVEL_VERBOSE;
    }
}

void GenieContext::Reset()
{
    if (GENIE_STATUS_SUCCESS != GenieDialog_reset(m_DialogHandle))
    {
        My_Log{} << "reset Genie Dialog failed\n";
    }
}
