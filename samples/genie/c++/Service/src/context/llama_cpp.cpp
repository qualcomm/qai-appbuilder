#include "llama_cpp.h"
#include <llama.h>
#include <arg.h>
#include <ggml-backend.h>
#include <sampling.h>
#include "core/log.h"
#include <filesystem>

namespace fs = std::filesystem;

#define LLAMA_BUILDER_DEBUG

class LLAMACppBuilder::Impl
{
public:
    explicit Impl(common_params &&params) : params_{std::move(params)}
    {
        params_.warmup = false;
        RegisterLogAdapter();

        if (params_.embedding)
        {
            throw std::runtime_error("embedding is not support yet");
        }

        if (params_.n_ctx != 0 && params_.n_ctx < 8)
        {
            params_.n_ctx = 8;
        }

        llama_backend_init();
        llama_numa_init(params_.numa);
        llama_init = common_init_from_params(params_);
        llama_model *model = llama_init.model.get();
        llama_context *ctx = llama_init.context.get();

        if (model == nullptr)
        {
            throw std::runtime_error("unable to load model");
        }

        vocab = llama_model_get_vocab(model);

        auto *cpu_dev = ggml_backend_dev_by_type(GGML_BACKEND_DEVICE_TYPE_CPU);
        if (!cpu_dev)
        {
            throw std::runtime_error("no CPU backend found");
        }
        auto *reg = ggml_backend_dev_backend_reg(cpu_dev);
        ggml_threadpool_new_fn = (decltype(ggml_threadpool_new) *) ggml_backend_reg_get_proc_address(reg,
                                                                                                     "ggml_threadpool_new");
        ggml_threadpool_free_fn = (decltype(ggml_threadpool_free) *) ggml_backend_reg_get_proc_address(reg,
                                                                                                       "ggml_threadpool_free");

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
        params_.conversation_mode = COMMON_CONVERSATION_MODE_ENABLED;

        if (!llama_model_has_encoder(model))
        {
            GGML_ASSERT(!llama_vocab_get_add_eos(vocab));
        }

        params_.interactive_first = true;
        params_.interactive = true;

        auto &sparams = params_.sampling;
        sparams.temp = 0.8;
        sparams.top_k = 40;
        sparams.top_p = 0.95;
        smpl = common_sampler_init(model, sparams);
        if (!smpl)
        {
            throw std::runtime_error("failed to initialize sampling subsystem");
        }
    }

    bool Query(const std::string &prompt, const std::function<bool(std::string &)> &callback)
    {
        {
            std::lock_guard<std::mutex> lk(m);
            is_interacting = true;
            done = false;
        }

        llama_context *ctx = llama_init.context.get();
        const int n_ctx = llama_n_ctx(ctx);
        bool input_echo = true;
        bool waiting_for_first_input = true;

        while (true)
        {
            int max_embd_size = n_ctx - 4;
            if ((int) embd.size() > max_embd_size)
            {
                embd.resize(max_embd_size);
            }

            if (n_past + (int) embd.size() >= n_ctx)
            {
                llama_memory_t mem = llama_get_memory(ctx);
                const int n_left = n_past - params_.n_keep;
                const int n_discard = n_left / 2;
                llama_memory_seq_rm(mem, 0, params_.n_keep, params_.n_keep + n_discard);
                llama_memory_seq_add(mem, 0, params_.n_keep + n_discard, n_past, -n_discard);
                n_past -= n_discard;
            }

            for (int i = 0; i < (int) embd.size(); i += params_.n_batch)
            {
                int n_eval = (int) embd.size() - i;
                if (n_eval > params_.n_batch)
                {
                    n_eval = params_.n_batch;
                }

                if (llama_decode(ctx, llama_batch_get_one(&embd[i], n_eval)))
                {
                    My_Log{My_Log::Level::kError} << "decode failed\n";
                    return false;
                }

                n_past += n_eval;
            }

            embd.clear();

            if ((int) embd_inp.size() <= n_consumed && !is_interacting)
            {
                const llama_token id = common_sampler_sample(smpl, ctx, -1);
                common_sampler_accept(smpl, id, true);
                embd.push_back(id);
                input_echo = true;
            }
            else
            {
                while ((int) embd_inp.size() > n_consumed)
                {
                    embd.push_back(embd_inp[n_consumed]);
                    common_sampler_accept(smpl, embd_inp[n_consumed], false);
                    ++n_consumed;
                    if ((int) embd.size() >= params_.n_batch)
                    {
                        break;
                    }
                }
            }

            if (input_echo)
            {
                for (auto id: embd)
                {
                    std::string token_str = common_token_to_piece(ctx, id, params_.special);
                    callback(token_str);
                }
            }

            if ((int) embd_inp.size() <= n_consumed)
            {
                if (!waiting_for_first_input && llama_vocab_is_eog(vocab, common_sampler_last(smpl)))
                {
                    is_interacting = true;
                }

                if ((n_past > 0 || waiting_for_first_input) && is_interacting)
                {
                    if (!waiting_for_first_input)
                    {
                        break;
                    }

                    const auto line_inp = common_tokenize(ctx, prompt, false, true);
                    llama_token eot = llama_vocab_eot(vocab);
                    embd_inp.push_back(eot == LLAMA_TOKEN_NULL ? llama_vocab_eos(vocab) : eot);
                    embd_inp.insert(embd_inp.end(), line_inp.begin(), line_inp.end());
                    input_echo = false;
                }

                if (n_past > 0 || waiting_for_first_input)
                {
                    if (is_interacting)
                    {
                        common_sampler_reset(smpl);
                    }
                    is_interacting = false;
                    waiting_for_first_input = false;
                }
            }
        }

        std::lock_guard<std::mutex> lk(m);
        done = true;
        cv.notify_one();
        return true;
    }

    ~Impl()
    {
        common_sampler_free(smpl);
        llama_backend_free();
        ggml_threadpool_free_fn(threadpool);
        ggml_threadpool_free_fn(threadpool_batch);
    }

    common_params params_;
    common_init_result llama_init;

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
    bool is_interacting = true;
    bool done{false};

private:
    static void RegisterLogAdapter()
    {
        llama_log_set([](ggml_log_level level, const char *text, void * /*user_data*/)
                      {
                          My_Log::Level my_level;
                          switch (level)
                          {
                              case GGML_LOG_LEVEL_ERROR:
                                  my_level = My_Log::Level::kError;
                                  break;
                              case GGML_LOG_LEVEL_WARN:
                                  my_level = My_Log::Level::kWarning;
                                  break;
                              case GGML_LOG_LEVEL_INFO:
                                  my_level = My_Log::Level::kInfo;
                                  break;
                              default:
                                  my_level = My_Log::Level::kVerbose;
                          }

                          My_Log{my_level} << text;
                      }, nullptr);
    }
};

LLAMACppBuilder::LLAMACppBuilder(const IModelConfig &info) :
        ContextBase{info}
{
    std::string gguf_path;
    for (const auto &entry: fs::directory_iterator(model_config_.get_model_path()))
    {
        if (entry.is_regular_file() && entry.path().extension() == ".gguf")
        {
            gguf_path = entry.path().string();
        }
    }

    common_params params;
    char *name[3]{"GenieService.exe", "--model" ,"gguf"};
    if (!common_params_parse(3, name, params, LLAMA_EXAMPLE_MAIN, nullptr))
    {
        throw std::runtime_error("common param parse failed");
    }
    params.model.path = gguf_path;
    impl_ = new Impl{std::move(params)};
}

bool LLAMACppBuilder::Query(const std::string &prompt, const Callback callback)
{
#ifdef LLAMA_BUILDER_DEBUG
    My_Log{} << "\n[Prompt]:\n"
             << prompt << "\n------------\n\n"
             << "[Response]:\n";
#endif

    return impl_->Query(prompt, callback);
}

LLAMACppBuilder::~LLAMACppBuilder()
{
    delete impl_;
    impl_ = nullptr;
}

bool LLAMACppBuilder::Stop()
{
    std::unique_lock<std::mutex> lk(impl_->m);
    impl_->is_interacting = false;
    impl_->cv.wait(lk, [this]
    { return impl_->done; });
    return true;
}

json LLAMACppBuilder::HandleProfile()
{
    return {};
}

size_t LLAMACppBuilder::TokenLength(const std::string &text)
{
    return impl_->embd_inp.size();
}
