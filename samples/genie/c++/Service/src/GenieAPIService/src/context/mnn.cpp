//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "mnn.h"

#include <llm/llm.hpp>
#include <MNN/AutoTime.hpp>
#include <MNN/expr/ExecutorScope.hpp>

#include <iomanip>
#include <sstream>

#include "log.h"
#include "utils.h"

#ifdef _WIN32
#include <windows.h>
#else
#include <sys/sysinfo.h>
#endif

// 权重文件大小 -> 预估运行时内存需求的经验系数（覆盖 KV cache/激活值等运行时开销）。
static constexpr double kMnnMemoryEstimateFactor = 1.3;
// 固定安全余量：覆盖 tokenizer/embedding 等未被 .mnn 扩展名匹配到的辅助文件及运行时开销（如 embeddings_bf16.bin）。
static constexpr uint64_t kMnnMemoryEstimateMarginBytes = 2ULL * 1024 * 1024 * 1024;
// 运行时低内存熔断阈值：加载前的内存预检查只在加载那一刻生效，无法覆盖长上下文下
// KV cache 在生成过程中持续增长的场景。生成循环内周期性采样可用物理内存，低于该
// 阈值时主动提前结束生成，把"被操作系统直接杀死进程"转化为服务自身可控的优雅停止。
static constexpr uint64_t kMnnLowMemoryAbortThresholdBytes = 1ULL * 1024 * 1024 * 1024;
// 每生成多少个 token 采样一次可用内存，避免 GlobalMemoryStatusEx 调用过于频繁带来额外开销。
static constexpr int kMnnLowMemoryCheckIntervalTokens = 16;

using namespace MNN::Transformer;

class MNNContext::Impl
{
public:
    std::unique_ptr<Llm> m_llm;
};

// Utf8StreamProcessor now lives in common/utils.h (shared with the GGUF backend, which needs
// the exact same per-token UTF-8 boundary buffering).

// https://github.com/alibaba/MNN/blob/master/apps/Android/MnnLlmChat/app/src/main/cpp/llm_stream_buffer.hpp
class LlmStreamBuffer : public std::streambuf
{
public:
    using CallBack = std::function<void(const char *str, size_t len)>;;

    explicit LlmStreamBuffer(CallBack callback) :
            m_callback(std::move(callback))
    {}

protected:
    std::streamsize xsputn(const char *s, std::streamsize n)

    override
    {
        if (m_callback)
        {
            m_callback(s, n);
        }
        return n;
    }

private:
    CallBack m_callback = nullptr;
};

uint64_t MNNContext::EstimateMnnMemoryRequirement(const std::string &model_path)
{
    uint64_t total_bytes = 0;
    std::vector<std::string> files;
    if (File::MatchFileInDir(model_path, ".mnn", &files))
    {
        for (const auto &file: files)
        {
            total_bytes += File::get_file_size(file, std::ios::binary);
        }
    }
    return static_cast<uint64_t>(static_cast<double>(total_bytes) * kMnnMemoryEstimateFactor) + kMnnMemoryEstimateMarginBytes;
}

uint64_t MNNContext::GetAvailablePhysicalMemoryBytes()
{
#ifdef _WIN32
    MEMORYSTATUSEX statex;
    statex.dwLength = sizeof(statex);
    if (GlobalMemoryStatusEx(&statex))
    {
        return statex.ullAvailPhys;
    }
#else
    struct sysinfo info{};
    if (sysinfo(&info) == 0)
    {
        return static_cast<uint64_t>(info.freeram) * info.mem_unit;
    }
#endif
    return UINT64_MAX;
}

MNNContext::MNNContext(const ModelInstanceConfig &config) :
        ContextBase(config)
{
    impl_ = new Impl{};
    MNN::BackendConfig backendConfig;
    auto executor = MNN::Express::Executor::newExecutor(MNN_FORWARD_CPU, backendConfig, 1);
    MNN::Express::ExecutorScope s(executor);
    std::string config_path = config.i_model_config_.get_config_path();
    impl_->m_llm = std::unique_ptr<Llm>(Llm::createLLM(config_path.c_str()));

    bool result = impl_->m_llm->load();
    if (!result)
    {
        throw std::runtime_error("MNNContext::MNNContext Load model failed.\n");
    }
}

MNNContext::~MNNContext()
{
    impl_->m_llm = nullptr;
    delete impl_;
    My_Log{} << "MNNContext::~MNNContext() Done。\n";
}

// TODO: run the query in thread to speed up the performance.
// https://github.com/alibaba/MNN/blob/master/transformers/llm/engine/demo/llm_demo.cpp
// https://github.com/alibaba/MNN/blob/master/transformers/llm/engine/include/llm/llm.hpp
// https://github.com/alibaba/MNN/blob/master/transformers/llm/engine/src/llm.cpp
bool MNNContext::Query(const ModelInput &model_input, const Callback& callback,
                       PrefillHeartbeatCallback /*prefill_heartbeat*/)
{
    // 注意：MNN 后端目前不支持 prefill 阶段心跳（MNN 推理接口不提供批次级回调）。
    // prefill_heartbeat 参数保留以满足接口兼容性，暂不使用。
    auto &prompt = model_input.text_;
    {
        const char *mnn_label = (model_input.agent_type_ == "main") ? "MAINAGENT_INFERENCE" : "SUBAGENT_INFERENCE";
        My_Log{} << "\n[Prompt] [" << mnn_label << "][" << model_config_.get_model_name() << "]:\n"
                 << prompt << "\n------------\n\n"
                 << "[Response] [" << mnn_label << "][" << model_config_.get_model_name() << "]:\n";
    }

    m_stop = false;
    stopped_by_output_limit_ = false;
    impl_->m_llm->reset();

    std::vector<int> input_ids = impl_->m_llm->tokenizer_encode(prompt);

    // Fix: 处理 callback 返回值（之前被忽略）
    // callback 返回 false 表示连接断开或请求停止，通过 m_stop 通知生成循环退出
    Utf8StreamProcessor processor([&](std::string &utf8Char)
                                  {
                                      if (!callback(utf8Char))
                                      {
                                          m_stop = true;
                                      }
                                  });

    LlmStreamBuffer stream_buffer{[&processor](const char *str, size_t len)
                                  {
                                      processor.processStream(str, len);
                                  }};

    std::ostream output_stream(&stream_buffer);
    impl_->m_llm->response(input_ids, &output_stream, nullptr, 0);

    // Fix: 使用 generate(1) 逐 token 生成，并计数以实现 max_length_ 限制
    // 每次 generate(1) 恰好生成一个 token，因此计数准确
    int n_generated = 0;
    while (!impl_->m_llm->stoped() && !m_stop)
    {
        impl_->m_llm->generate(1);
        n_generated++;
        // Fix: 检查是否达到最大输出 token 数
        if (max_length_ > 0 && n_generated >= max_length_)
        {
            My_Log{My_Log::Level::kWarning}
                << "[MNN] Generated " << n_generated
                << " tokens, reached max output limit " << max_length_
                << ". Stopping generation." << std::endl;
            stopped_by_output_limit_ = true;
            m_stop = true;
        }
        else if (n_generated % kMnnLowMemoryCheckIntervalTokens == 0 &&
                 GetAvailablePhysicalMemoryBytes() < kMnnLowMemoryAbortThresholdBytes)
        {
            // 加载前的内存预检查只在加载那一刻生效,无法覆盖长上下文下 KV cache 在生成
            // 过程中持续增长导致的运行时 OOM。这里主动提前结束生成(不设置
            // stopped_by_output_limit_,避免误标 finish_reason="length" 或触发云端
            // overflow 回退逻辑),把"被操作系统直接杀死进程"转化为可控的优雅停止。
            My_Log{My_Log::Level::kError}
                << "[MNN] Available physical memory dropped below "
                << kMnnLowMemoryAbortThresholdBytes << " bytes after generating "
                << n_generated << " tokens. Aborting generation early to avoid an OS-level OOM kill."
                << std::endl;
            m_stop = true;
        }
    }

    m_stop = false;

    output_stream.flush();

    return true;
}

bool MNNContext::Stop()
{
    m_stop = true;
    return true;
}

size_t MNNContext::TokenLength(const std::string &text)
{
    // 使用 MNN LLM 的 tokenizer 对文本进行编码，返回真实的 token 数。
    // 基类默认实现返回 text.size()（字符数），对中文等多字节字符不准确，
    // 会导致 Fix 1 中 available_output = context_size - prompt_tokens 计算偏差。
    if (!impl_ || !impl_->m_llm)
    {
        // 模型未加载时退化为字符数（与基类行为一致）
        return text.size();
    }
    try
    {
        std::vector<int> tokens = impl_->m_llm->tokenizer_encode(text);
        return tokens.size();
    }
    catch (const std::exception &e)
    {
        My_Log{My_Log::Level::kWarning}
            << "[MNN] TokenLength failed: " << e.what()
            << ", falling back to text.size()" << std::endl;
        return text.size();
    }
}

json MNNContext::HandleProfile()
{
    // 从 LlmContext 中读取性能数据（LlmContext 由 MNN LLM 在推理过程中填充）
    // LlmContext 字段说明（来自 llm.hpp）：
    //   prompt_len  : prompt token 数
    //   gen_seq_len : 生成 token 数
    //   prefill_us  : prefill 阶段耗时（微秒）
    //   decode_us   : decode 阶段耗时（微秒）
    const LlmContext *ctx = impl_->m_llm->getContext();
    if (!ctx)
    {
        return {};
    }

    json result;
    std::ostringstream oss;

    // time_to_first_token: prefill_us (μs) → s
    double prefill_s = static_cast<double>(ctx->prefill_us) / 1000000.0;
    oss << std::fixed << std::setprecision(2) << prefill_s;
    result["time_to_first_token"] = oss.str();

    // token_generation_time: decode_us (μs) → s
    oss.str("");
    double decode_s = static_cast<double>(ctx->decode_us) / 1000000.0;
    oss << std::fixed << std::setprecision(2) << decode_s;
    result["token_generation_time"] = oss.str();

    // prompt_processing_rate: prompt_len / prefill_s (tok/s)
    oss.str("");
    double prefill_rate = (prefill_s > 0.0 && ctx->prompt_len > 0)
                          ? static_cast<double>(ctx->prompt_len) / prefill_s
                          : 0.0;
    oss << std::fixed << std::setprecision(2) << prefill_rate;
    result["prompt_processing_rate"] = oss.str();

    // token_generation_rate: gen_seq_len / decode_s (tok/s)
    oss.str("");
    double decode_rate = (decode_s > 0.0 && ctx->gen_seq_len > 0)
                         ? static_cast<double>(ctx->gen_seq_len) / decode_s
                         : 0.0;
    oss << std::fixed << std::setprecision(2) << decode_rate;
    result["token_generation_rate"] = oss.str();

    // integer values
    result["num_prompt_tokens"] = ctx->prompt_len;
    result["num_generated_tokens"] = ctx->gen_seq_len;

    return result;
}
