//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "llama_cpp.h"
#include <llama.h>
#include <arg.h>
#include <ggml-backend.h>
#include <sampling.h>
#include "log.h"
#include "utils.h"
#include <filesystem>
#include <sstream>
#include <iomanip>
#include <algorithm>
#include <cctype>
#include <thread>
#include <chrono>

namespace fs = std::filesystem;

// ─────────────────────────────────────────────────────────────────────────────
// 返回自 epoch 以来的毫秒数（用于 prefill/生成速率等性能日志的时间戳计算）
// ─────────────────────────────────────────────────────────────────────────────
static inline int64_t now_ms()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

class LLAMACppBuilder::Impl
{
public:
    explicit Impl(common_params &&params) : params_{std::move(params)}
    {
        params_.warmup = false;

        if (params_.embedding)
        {
            throw std::runtime_error("embedding is not support yet");
        }

        if (params_.n_ctx != 0 && params_.n_ctx < 8)
        {
            params_.n_ctx = 4096;
        }

        common_init();

        RegisterLogAdapter();

        llama_backend_init();
        llama_numa_init(params_.numa);

        llama_init = common_init_from_params(params_);
        llama_model *model = llama_init->model();
        llama_context *ctx = llama_init->context();

        if (model == nullptr)
        {
            throw std::runtime_error("unable to load model");
        }

        vocab = llama_model_get_vocab(model);

        smpl = llama_init->sampler(0);
        if (!smpl)
        {
            throw std::runtime_error("failed to initialize sampling subsystem");
        }

        // 线程池创建与绑定，对齐 completion.cpp:163-202
        auto *cpu_dev = ggml_backend_dev_by_type(GGML_BACKEND_DEVICE_TYPE_CPU);
        if (!cpu_dev)
        {
            throw std::runtime_error("no CPU backend found");
        }
        auto *reg = ggml_backend_dev_backend_reg(cpu_dev);
        ggml_threadpool_new_fn = (decltype(ggml_threadpool_new) *) ggml_backend_reg_get_proc_address(reg, "ggml_threadpool_new");
        ggml_threadpool_free_fn = (decltype(ggml_threadpool_free) *) ggml_backend_reg_get_proc_address(reg, "ggml_threadpool_free");

        struct ggml_threadpool_params tpp_batch =
                ggml_threadpool_params_from_cpu_params(params_.cpuparams_batch);
        struct ggml_threadpool_params tpp =
                ggml_threadpool_params_from_cpu_params(params_.cpuparams);

        set_process_priority(params_.cpuparams.priority);

        if (!ggml_threadpool_params_match(&tpp, &tpp_batch))
        {
            threadpool_batch = ggml_threadpool_new_fn(&tpp_batch);
            if (!threadpool_batch)
            {
                throw std::runtime_error("threadpool create failed with tpp");
            }
            tpp.paused = true;
        }

        threadpool = ggml_threadpool_new_fn(&tpp);
        if (!threadpool)
        {
            throw std::runtime_error("threadpool create failed");
        }

        llama_attach_threadpool(ctx, threadpool, threadpool_batch);

        // 重置性能计数器，从这里开始测量（对齐 completion.cpp:161）
        llama_perf_context_reset(ctx);

        if (!llama_model_has_encoder(model))
        {
            GGML_ASSERT(!llama_vocab_get_add_eos(vocab));
        }
    }

    bool Query(const std::string &prompt,
               const std::function<bool(std::string &)> &callback,
               const std::function<bool()> &prefill_heartbeat,
               const char *query_type, bool is_aux_inference)
    {
        auto t_query_start = std::chrono::steady_clock::now();

        // 截取 prompt 前 100 字符作为标识（安全检查 prompt 以 "You are a security classifier" 开头）
        std::string prompt_prefix = prompt.substr(0, std::min(prompt.size(), size_t(100)));
        for (auto &c : prompt_prefix) if (c == '\n') c = ' ';

        My_Log{} << "[LLAMACpp] " << query_type << " Query START"
                 << " | prompt_chars=" << prompt.size()
                 << " | prompt_prefix=\"" << prompt_prefix << "\""
                 << " @" << My_Log::GetTimeString() << "\n";

        {
            std::lock_guard<std::mutex> lk(m);
            done = false;
        }

        // Fix: 重置输出限制状态
        stopped_by_output_limit_ = false;
        int n_generated = 0;
        bool should_stop = false;

        // GenieAPIService 特有：每次 Query() 调用都要重置状态，因为同一个 Impl 实例要
        // 串行处理多个独立请求（completion.cpp 是单进程单次运行，从不需要这个）。
        n_past = 0;
        n_consumed = 0;
        embd.clear();
        embd_inp.clear();

        common_sampler_reset(smpl);

        llama_context *ctx = llama_init->context();
        llama_memory_t mem = llama_get_memory(ctx);
        llama_memory_clear(mem, false);

        // 重置性能计数器，确保每次 Query 的统计数据是独立的
        llama_perf_context_reset(ctx);

        const int n_ctx = llama_n_ctx(ctx);

        // tokenize prompt，对齐 completion.cpp:318-320
        const bool add_bos = llama_vocab_get_add_bos(vocab) && !params_.use_jinja;
        embd_inp = common_tokenize(ctx, prompt, true, true);

        if (embd_inp.empty())
        {
            // 对齐 completion.cpp:330-339：空 prompt 时退化为仅一个 BOS token，否则报错。
            if (add_bos)
            {
                embd_inp.push_back(llama_vocab_bos(vocab));
            }
            else
            {
                My_Log{My_Log::Level::kError} << "[LLAMACpp] " << query_type << " input is empty\n";
                std::lock_guard<std::mutex> lk(m);
                done = true;
                cv.notify_one();
                return false;
            }
        }

        My_Log{} << "[LLAMACpp] " << query_type
                 << " Tokenization done: " << embd_inp.size() << " tokens (add_bos=" << add_bos << ")\n";

        // n_keep 是本次调用范围内的局部值，不写回 params_，对齐 completion.cpp:399-404
        int n_keep;
        if (params_.n_keep < 0 || params_.n_keep > (int) embd_inp.size())
        {
            n_keep = (int) embd_inp.size();
        }
        else
        {
            n_keep = params_.n_keep + (add_bos ? 1 : 0);
        }

        int n_remain = params_.n_predict;

        bool prefill_done = false;
        int64_t t_prefill_start_ms = 0;
        int64_t t_prefill_end_ms = 0;
        int total_prefill_tokens = 0;
        int total_decode_batches = 0;
        int64_t t_gen_start_ms = 0;
        std::string generated_text_buf;  // 累积生成文本（用于日志）

        // Prefill 阶段心跳计时：记录上次发送心跳的时间点
        // 每隔 kPrefillHeartbeatIntervalMs 毫秒向客户端发送一次保活消息，
        // 防止客户端或中间代理因 prefill 期间长时间无数据而超时断开连接。
        auto t_last_prefill_heartbeat = std::chrono::steady_clock::now();
        constexpr int64_t kPrefillHeartbeatIntervalMs = 5000; // 每 5 秒发一次心跳

        // common_token_to_piece() emits text one token at a time, and a multi-byte UTF-8
        // character can legitimately be split across two tokens. Buffer until the character is
        // complete before forwarding to callback, otherwise a truncated tail reaches the SSE
        // JSON encoder as invalid UTF-8 (garbled output, or an assertion in a debug-enabled build).
        Utf8StreamProcessor token_utf8_processor([&](std::string &complete_chars)
                                                  {
                                                      if (!callback(complete_chars))
                                                      {
                                                          should_stop = true;
                                                      }
                                                  });

        // 线性推理主循环，对齐 completion.cpp 非交互（-no-cnv）分支：溢出平移 → 分批 decode → 采样 → 回显 → EOG/n_predict 退出
        while (n_remain != 0)
        {
            if (!embd.empty())
            {
                int max_embd_size = n_ctx - 4;
                if ((int) embd.size() > max_embd_size)
                {
                    embd.resize(max_embd_size);
                }

                // 上下文溢出时的 n_keep 平移（对齐 completion.cpp:609-637）
                if (n_past + (int) embd.size() >= n_ctx)
                {
                    const int n_left = n_past - n_keep;
                    const int n_discard = n_left / 2;
                    llama_memory_seq_rm(mem, 0, n_keep, n_keep + n_discard);
                    llama_memory_seq_add(mem, 0, n_keep + n_discard, n_past, -n_discard);
                    n_past -= n_discard;
                    My_Log{My_Log::Level::kWarning}
                        << "[LLAMACpp] " << query_type
                        << " Context overflow: discarded " << n_discard << " tokens, n_past=" << n_past << "\n";
                }

                // 按 n_batch（非 n_ubatch）分批 decode，对齐 completion.cpp:684-703
                if (!prefill_done && t_prefill_start_ms == 0)
                {
                    t_prefill_start_ms = now_ms();
                }

                if (llama_decode(ctx, llama_batch_get_one(embd.data(), (int) embd.size())))
                {
                    My_Log{My_Log::Level::kError} << "[LLAMACpp] " << query_type << " llama_decode FAILED\n";
                    return false;
                }

                n_past += (int) embd.size();
                total_decode_batches++;

                if (!prefill_done)
                {
                    total_prefill_tokens += (int) embd.size();

                    // Prefill 阶段心跳：每隔 kPrefillHeartbeatIntervalMs 毫秒向客户端发送一次保活消息，
                    // 防止中间代理因长时间无数据而超时断开；heartbeat 返回 false 表示客户端已断开，立即中止 prefill。
                    if (prefill_heartbeat)
                    {
                        auto t_now_hb = std::chrono::steady_clock::now();
                        int64_t elapsed_hb = std::chrono::duration_cast<std::chrono::milliseconds>(
                            t_now_hb - t_last_prefill_heartbeat).count();
                        if (elapsed_hb >= kPrefillHeartbeatIntervalMs)
                        {
                            t_last_prefill_heartbeat = t_now_hb;
                            My_Log{} << "[LLAMACpp] " << query_type
                                     << " Prefill heartbeat: batch #" << total_decode_batches
                                     << ", n_past=" << n_past
                                     << ", elapsed_ms=" << elapsed_hb << "\n";
                            if (!prefill_heartbeat())
                            {
                                // 连接已断开，立即中止 prefill
                                My_Log{My_Log::Level::kWarning}
                                    << "[LLAMACpp] " << query_type
                                    << " Prefill aborted: connection broken at n_past=" << n_past
                                    << " (batch #" << total_decode_batches << ")\n";
                                {
                                    std::lock_guard<std::mutex> lk(m);
                                    done = true;
                                    // 必须 notify，否则若外部调用 Stop() 等待 done==true，
                                    // 将因无通知而永久阻塞（cv.wait 不会自动唤醒）。
                                    cv.notify_one();
                                }
                                return false;
                            }
                        }
                    }
                }
            }

            embd.clear();

            // is_generated_token：区分"采样出的新 token"与"转发摄入的 prompt token"，仅回显前者
            bool is_generated_token = false;
            if ((int) embd_inp.size() <= n_consumed)
            {
                if (!prefill_done)
                {
                    prefill_done = true;
                    t_prefill_end_ms = now_ms();
                    t_gen_start_ms = t_prefill_end_ms;
                }

                const llama_token id = common_sampler_sample(smpl, ctx, -1);
                common_sampler_accept(smpl, id, true);
                embd.push_back(id);
                is_generated_token = true;
                --n_remain;
            }
            else
            {
                while ((int) embd_inp.size() > n_consumed)
                {
                    embd.push_back(embd_inp[n_consumed]);
                    common_sampler_accept(smpl, embd_inp[n_consumed], false);
                    ++n_consumed;
                    if ((int) embd.size() == params_.n_batch)
                    {
                        break;
                    }
                }
            }

            // 仅回显生成出的 token，绝不回显 prompt 本身（与 completion.cpp 作为 CLI 会打印 prompt 不同）
            if (is_generated_token)
            {
                for (auto id: embd)
                {
                    std::string token_str = common_token_to_piece(ctx, id, params_.special);
                    generated_text_buf += token_str;

                    // Fix: 处理 callback 返回值（之前被忽略）；经 token_utf8_processor 缓冲后才
                    // 转交 callback，避免跨 token 边界被截断的多字节字符以非法 UTF-8 传下去。
                    token_utf8_processor.processStream(token_str.data(), token_str.size());
                    if (should_stop)
                    {
                        break;
                    }
                    n_generated++;
                    // Fix: 检查是否达到最大输出 token 数
                    if (max_length_ > 0 && n_generated >= max_length_)
                    {
                        My_Log{My_Log::Level::kWarning}
                            << "[LLAMACpp] " << query_type
                            << " Generated " << n_generated
                            << " tokens, reached max output limit " << max_length_
                            << ". Stopping generation." << std::endl;
                        stopped_by_output_limit_ = true;
                        should_stop = true;
                        break;
                    }
                }
            }

            if (should_stop)
            {
                My_Log{}.original(true) << "\n\n";
                My_Log{} << "[LLAMACpp] " << query_type
                         << " Query stopped by callback/limit after " << n_generated << " tokens\n";
                break;
            }

            // 结束生成判断：非交互模式下的单一退出点，对齐 completion.cpp:970
            if (is_generated_token && llama_vocab_is_eog(vocab, embd.back()))
            {
                My_Log{}.original(true) << "\n";
                My_Log{} << "[LLAMACpp] " << query_type
                         << " EOG token detected after " << n_generated << " tokens. Stopping.\n";
                break;
            }
        }

        auto t_query_end = std::chrono::steady_clock::now();
        int64_t total_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
            t_query_end - t_query_start).count();
        int64_t gen_elapsed_ms = (t_gen_start_ms > 0) ? (now_ms() - t_gen_start_ms) : 0;
        double gen_rate = (gen_elapsed_ms > 0 && n_generated > 0) ?
            (n_generated * 1000.0 / gen_elapsed_ms) : 0.0;

        llama_perf_context_data perf_ctx = llama_perf_context(ctx);
        double prompt_rate_llama = (perf_ctx.t_p_eval_ms > 0) ?
            (perf_ctx.n_p_eval * 1000.0) / perf_ctx.t_p_eval_ms : 0.0;
        double gen_rate_llama = (perf_ctx.t_eval_ms > 0) ?
            (perf_ctx.n_eval * 1000.0) / perf_ctx.t_eval_ms : 0.0;

        My_Log{} << "[LLAMACpp] " << query_type << " Query DONE"
                 << " | total_ms=" << total_ms
                 << " | prefill_tokens=" << total_prefill_tokens
                 << " | gen_tokens=" << n_generated
                 << " | gen_rate=" << std::fixed << std::setprecision(1) << gen_rate << " tok/s"
                 << " | llama_prefill=" << std::fixed << std::setprecision(1) << prompt_rate_llama << " tok/s"
                 << " | llama_gen=" << std::fixed << std::setprecision(1) << gen_rate_llama << " tok/s\n";

        if (is_aux_inference && !generated_text_buf.empty()) {
            // 打印完整生成文本（辅助推理结果）
            My_Log{} << "[LLAMACpp] " << query_type
                     << " Full generated text: \"" << generated_text_buf << "\"\n";
        }

        std::lock_guard<std::mutex> lk(m);
        done = true;
        cv.notify_one();
        return true;
    }

    ~Impl()
    {
        {
            std::lock_guard<std::mutex> lk(m);
            done = true;
        }
        cv.notify_all();

        std::this_thread::sleep_for(std::chrono::milliseconds(100));

        // 先分离 threadpool，再释放（顺序必须）
        llama_context *ctx = llama_init ? llama_init->context() : nullptr;
        if (ctx)
        {
            llama_attach_threadpool(ctx, nullptr, nullptr);
        }

        if (threadpool)
        {
            ggml_threadpool_free_fn(threadpool);
            threadpool = nullptr;
        }
        if (threadpool_batch)
        {
            ggml_threadpool_free_fn(threadpool_batch);
            threadpool_batch = nullptr;
        }

        // llama_init 释放时会自动释放 model/context/sampler，sampler 的所有权属于 llama_init，
        // 不要手动释放。
        if (llama_init)
        {
            llama_init.reset();
            smpl = nullptr;
        }

        llama_backend_free();
    }

    common_params params_;
    common_init_result_ptr llama_init;

    ggml_threadpool *(*ggml_threadpool_new_fn)(ggml_threadpool_params *){};

    void (*ggml_threadpool_free_fn)(ggml_threadpool *){};

    const llama_vocab *vocab = nullptr;
    common_sampler *smpl = nullptr;
    ggml_threadpool *threadpool_batch{};
    ggml_threadpool *threadpool{};

    int n_past = 0;
    int n_consumed = 0;

    std::vector<llama_token> embd;
    std::vector<llama_token> embd_inp;

    std::mutex m;
    std::condition_variable cv;
    bool done{false};

    // Fix: 最大输出 token 数（由 SetParams 设置，默认 4096）
    int max_length_ = 4096;
    // Fix: 是否因输出 token 限制而停止生成
    bool stopped_by_output_limit_ = false;

private:
    // 把 ggml/llama 内部日志按 ggml_log_level 映射到项目统一日志体系（My_Log::Level）转发，
    // 遵循项目默认日志级别（kWarning）过滤，不再使用裸时间戳 fprintf 直接写 stderr。
    static void RegisterLogAdapter()
    {
        llama_log_set([](ggml_log_level level, const char *text, void * /*user_data*/)
                    {
                        My_Log::Level mapped_level;
                        switch (level)
                        {
                            case GGML_LOG_LEVEL_ERROR:
                                mapped_level = My_Log::Level::kError;
                                break;
                            case GGML_LOG_LEVEL_WARN:
                                mapped_level = My_Log::Level::kWarning;
                                break;
                            case GGML_LOG_LEVEL_INFO:
                                mapped_level = My_Log::Level::kInfo;
                                break;
                            case GGML_LOG_LEVEL_DEBUG:
                                mapped_level = My_Log::Level::kDebug;
                                break;
                            default:
                                mapped_level = My_Log::Level::kInfo;
                                break;
                        }
                        My_Log{mapped_level} << text;
                    }, nullptr);
    }
};

LLAMACppBuilder::LLAMACppBuilder(const ModelInstanceConfig &config) :
        ContextBase{config}
{
    // 在初始化 llama.cpp 后端之前设置 OpenCL Adreno 大缓冲区环境变量
    // 等效于命令行执行前设置: set GGML_OPENCL_ADRENO_USE_LARGE_BUFFER=1
#if defined(_WIN32)
    _putenv_s("GGML_OPENCL_ADRENO_USE_LARGE_BUFFER", "1");
#else
    setenv("GGML_OPENCL_ADRENO_USE_LARGE_BUFFER", "1", 1);
#endif
    My_Log{} << "[Env] GGML_OPENCL_ADRENO_USE_LARGE_BUFFER=1 set\n";

    std::string gguf_path;
    for (const auto &entry: fs::directory_iterator(model_config_.get_model_path()))
    {
        if (entry.is_regular_file() && entry.path().extension() == ".gguf")
        {
            gguf_path = entry.path().string();
        }
    }

    // 上下文窗口大小：未配置时使用默认值
    size_t configured_context_size = model_config_.get_context_size();
    int n_ctx = configured_context_size > 0 ? static_cast<int>(configured_context_size) : 8192;

    std::string device = model_config_.get_device();
    std::transform(device.begin(), device.end(), device.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });

    // 直接构造与 llama-completion.exe 完全等价的命令行参数，交给 common_params_parse 走与
    // CLI 工具完全相同的解析/后处理路径——避免手动逐字段赋值遗漏只在参数解析时才生效的设置
    // （如 --device），GPU/CPU 取值均已用 llama-completion.exe 反复 A/B 实测验证为最快组合。
    std::vector<std::string> arg_strings = {
        "GenieService.exe",
        "--model", gguf_path,
        "--ctx-size", std::to_string(n_ctx),
        "--no-warmup",
        "-no-cnv",
        "-s", "42",
        "--fit", "off",
        "-fa", "on",
    };
    if (device == "cpu")
    {
        // CPU 路径：保持 mmap/repack/no-host 的 llama.cpp 官方默认值（已实测验证比强制关闭更快）。
        arg_strings.insert(arg_strings.end(), {"-ngl", "0"});
    }
    else
    {
        arg_strings.insert(arg_strings.end(), {
            "--device", "GPUOpenCL",
            "-ngl", "99",
            "--no-mmap",
            "--no-repack",
            "--no-host",
            "-ub", "1024",
        });
    }

    std::vector<char *> argv;
    argv.reserve(arg_strings.size());
    for (auto &s: arg_strings)
    {
        argv.push_back(s.data());
    }

    common_params params;
    if (!common_params_parse((int) argv.size(), argv.data(), params, LLAMA_EXAMPLE_COMPLETION, nullptr))
    {
        throw std::runtime_error("common param parse failed");
    }

    params.verbosity = 0;           // 禁用详细日志

    // 线程数：运行时动态获取物理/逻辑核心数，替代旧的硬编码值 10，零配置适配不同机器
    // （已通过专项测试确认：GPU 全量卸载场景下线程数在合理范围内影响很小，CPU 场景下
    // 用满核心数是稳妥的默认策略）。
    const auto hw_threads = static_cast<int32_t>(std::thread::hardware_concurrency());
    params.cpuparams.n_threads = hw_threads > 0 ? hw_threads : 4;
    params.cpuparams_batch.n_threads = params.cpuparams.n_threads;

    // 生成配置：不设固定上限，交由 SetParams("size", ...) 驱动的 max_length_ 来控制输出长度上限。
    params.n_predict = -1;

    // Harmony 格式使用 <|return|>/<|call|> 等特殊 token 作为解码时停止标记；special=false（默认值）
    // 会让 common_token_to_piece() 对特殊 token 返回空串，导致 HarmonyProcessor 无法识别结束标记。
    params.special = true;

    My_Log{} << "[LLAMACpp] Loaded params: device=" << device
             << ", n_ctx=" << params.n_ctx
             << ", n_gpu_layers=" << params.n_gpu_layers
             << ", use_mmap=" << params.use_mmap
             << ", no_extra_bufts=" << params.no_extra_bufts
             << ", no_host=" << params.no_host
             << ", n_batch=" << params.n_batch
             << ", n_ubatch=" << params.n_ubatch
             << ", n_threads=" << params.cpuparams.n_threads
             << "\n";

    impl_ = new Impl{std::move(params)};
}

int LLAMACppBuilder::SetParams(const std::string &key, const std::string &value)
{
    if (impl_ && key == "size")
    {
        impl_->max_length_ = std::stoi(value);
        My_Log{My_Log::Level::kInfo}
            << "[LLAMACpp] SetParams: max_length=" << impl_->max_length_ << std::endl;
    }
    return 0;
}

bool LLAMACppBuilder::Query(const ModelInput &model_input, const Callback& callback,
                             PrefillHeartbeatCallback prefill_heartbeat)
{
    // 序列化所有 Query() 调用：llama_context 不是线程安全的，同一时刻只能有一个
    // 推理在执行。安全检查（ContentSecurityInspector）、复杂度评估
    // （TaskComplexityEvaluator）和正常推理都通过同一个模型句柄调用 Query()，
    // 必须通过此锁保证互斥，防止并发调用 llama_decode 导致 KV cache 损坏崩溃。

    // 检测锁竞争：等待超过 10ms 视为异常，记录警告
    auto t_lock_wait_start = std::chrono::steady_clock::now();
    std::lock_guard<std::mutex> lock(query_mutex_);
    int64_t lock_wait_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - t_lock_wait_start).count();
    if (lock_wait_ms > 10)
    {
        My_Log{My_Log::Level::kWarning}
            << "[LLAMACpp] query_mutex_ wait time: " << lock_wait_ms << "ms (lock contention detected)\n";
    }

    auto &prompt = model_input.text_;

    // 检测查询类型（同时供本函数日志与 Impl::Query 内部日志复用，避免对同一个 prompt 重复扫描）
    const char *query_type_label;
    bool is_aux_inference;
    if (prompt.find("You are a security classifier") != std::string::npos)
    {
        query_type_label = "SECURITY_CHECK";
        is_aux_inference = true;
    }
    else if (prompt.find("You are a task complexity classifier") != std::string::npos)
    {
        query_type_label = "COMPLEXITY_CHECK";
        is_aux_inference = true;
    }
    else if (prompt.find("You are a redaction assistant") != std::string::npos)
    {
        query_type_label = "DESENSITIZE";
        is_aux_inference = true;
    }
    // 使用 ModelInput 中由 ModelInputBuilder 预先检测并设置的 agent_type_ 字段
    else if (model_input.agent_type_ == "main")
    {
        query_type_label = "MAINAGENT_INFERENCE";
        is_aux_inference = false;
    }
    else
    {
        query_type_label = "SUBAGENT_INFERENCE";
        is_aux_inference = false;
    }

    if (!is_aux_inference)
    {
        My_Log{} << "\n[Prompt] [" << query_type_label << "][" << model_config_.get_model_name() << "]:\n"
                 << prompt << "\n------------\n\n"
                 << "[Response] [" << query_type_label << "][" << model_config_.get_model_name() << "]:\n";
    }
    else
    {
        My_Log{} << "\n[Prompt] [" << query_type_label << "]:\n"
                 << prompt << "\n------------\n\n"
                 << "[Response] [" << query_type_label << "]:\n";
    }

    std::string query_type_bracketed = std::string("[") + query_type_label + "]";
    bool result = impl_->Query(prompt, callback, prefill_heartbeat,
                                query_type_bracketed.c_str(), is_aux_inference);
    // Fix: 将 Impl 内部的 stopped_by_output_limit_ 状态传播到外部
    stopped_by_output_limit_ = impl_->stopped_by_output_limit_;
    return result;
}

LLAMACppBuilder::~LLAMACppBuilder()
{
    delete impl_;
    impl_ = nullptr;
}

bool LLAMACppBuilder::Stop()
{
    std::unique_lock<std::mutex> lk(impl_->m);
    impl_->cv.wait(lk, [this]
    { return impl_->done; });
    return true;
}

json LLAMACppBuilder::HandleProfile()
{
    json result;
    
    if (impl_)
    {
        llama_context *ctx = impl_->llama_init->context();
        if (!ctx)
        {
            return result;
        }
        
        // 从 llama.cpp 获取性能统计数据
        llama_perf_context_data perf_ctx = llama_perf_context(ctx);
        
        std::ostringstream oss;
        
        // time_to_first_token (prompt 处理时间,转换为秒)
        oss << std::fixed << std::setprecision(2)
            << perf_ctx.t_p_eval_ms / 1000.0;
        result["time_to_first_token"] = oss.str();
        
        // token_generation_time (生成时间,转换为秒)
        oss.str("");
        oss << std::fixed << std::setprecision(2)
            << perf_ctx.t_eval_ms / 1000.0;
        result["token_generation_time"] = oss.str();
        
        // prompt_processing_rate (tokens/秒)
        oss.str("");
        oss << std::fixed << std::setprecision(2);
        if (perf_ctx.t_p_eval_ms > 0)
        {
            oss << (perf_ctx.n_p_eval * 1000.0) / perf_ctx.t_p_eval_ms;
        }
        else
        {
            oss << 0.0;
        }
        result["prompt_processing_rate"] = oss.str();
        
        // token_generation_rate (tokens/秒)
        oss.str("");
        oss << std::fixed << std::setprecision(2);
        if (perf_ctx.t_eval_ms > 0)
        {
            oss << (perf_ctx.n_eval * 1000.0) / perf_ctx.t_eval_ms;
        }
        else
        {
            oss << 0.0;
        }
        result["token_generation_rate"] = oss.str();
        
        // integer values
        result["num_prompt_tokens"] = perf_ctx.n_p_eval;
        result["num_generated_tokens"] = perf_ctx.n_eval;
    }
    
    return result;
}

size_t LLAMACppBuilder::TokenLength(const std::string &text)
{
    // 对输入文本进行 tokenize，并返回 token 数。
    // 注意：embd_inp 是对话/推理过程中的输入缓冲，和外部传入 text 无关。
    // 这里必须基于当前模型/上下文的 vocab 来做编码。
    llama_context *ctx = impl_->llama_init->context();
    if (ctx == nullptr)
    {
        return 0;
    }

    // common_tokenize 会基于 ctx 的 vocab 进行编码。
    // add_special=false: 不额外添加 BOS/EOS 等特殊 token（仅统计 text 自身的编码长度）
    // parse_special=true: 允许文本中包含类似 <|...|> 这类特殊 token 时按 token 解析
    const auto tokens = common_tokenize(ctx, text, /* add_special = */ false, /* parse_special = */ true);
    return tokens.size();
}
