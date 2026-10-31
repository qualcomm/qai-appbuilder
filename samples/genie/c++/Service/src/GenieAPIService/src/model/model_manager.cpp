//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "log.h"
#include "utils.h"
#include "def.h"
#include "model_manager.h"
#include "../context/llama_speculative_config.h"
#include "../context/qnn/genie.h"
#include "../context/mnn.h"
#include "../context/llama_cpp.h"
#include "../response/response_tools.h"
#include "../chat_request_handler/summary_cache.h"
#include "../chat_request_handler/task_memo_store.h"
#include "../chat_request_handler/tool_call_circuit_breaker_store.h"
#include "../chat_request_handler/tool_call_repetition_store.h"
#include "../chat_request_handler/request_trace_writer.h"
#include <algorithm>
#include <cctype>
#include <filesystem>
#include <thread>
#include <chrono>

#ifdef _WIN32

#include <wincred.h>

#endif

namespace fs = std::filesystem;

static std::string LoadApiKeyFromCredentialManager(const std::string &account)
{
#ifdef _WIN32
    const std::wstring service_name = L"QAIAgentForge";
    const std::wstring account_w(account.begin(), account.end());

    auto try_read = [&](const std::wstring &target) -> std::string
    {
        PCREDENTIALW pcred = nullptr;
        if (!CredReadW(target.c_str(), CRED_TYPE_GENERIC, 0, &pcred) || pcred == nullptr)
            return "";

        std::string result;
        bool user_match = (pcred->UserName != nullptr &&
                           std::wstring(pcred->UserName) == account_w);

        if (user_match && pcred->CredentialBlobSize > 0 && pcred->CredentialBlob != nullptr)
        {
            const wchar_t *blob_w = reinterpret_cast<const wchar_t *>(pcred->CredentialBlob);
            int blob_wchars = static_cast<int>(pcred->CredentialBlobSize / sizeof(wchar_t));
            int utf8_len = WideCharToMultiByte(CP_UTF8, 0, blob_w, blob_wchars,
                                               nullptr, 0, nullptr, nullptr);
            if (utf8_len > 0)
            {
                result.resize(utf8_len);
                WideCharToMultiByte(CP_UTF8, 0, blob_w, blob_wchars,
                                    &result[0], utf8_len, nullptr, nullptr);
            }
        }
        CredFree(pcred);
        return result;
    };

    std::string key = try_read(service_name);
    if (!key.empty())
    {
        My_Log{} << "[CredMgr] API Key loaded from Credential Manager (primary target): account=" << account
                 << std::endl;
        return key;
    }

    std::wstring compound_target = account_w + L"@" + service_name;
    key = try_read(compound_target);
    if (!key.empty())
    {
        My_Log{} << "[CredMgr] API Key loaded from Credential Manager (compound target): account=" << account
                 << std::endl;
        return key;
    }
#endif
    return "";
}

static std::string ResolveApiKey(const std::string &json_api_key, const std::string &account)
{
    std::string cm_key = LoadApiKeyFromCredentialManager(account);
    if (!cm_key.empty())
    {
        return cm_key;
    }
    if (!json_api_key.empty())
    {
        My_Log{} << "[CredMgr] API Key fallback to JSON value: account=" << account << std::endl;
    }
    return json_api_key;
}

#include <LibAppBuilder.hpp>

static void ParsePromptFile(const std::string &prompt_path,
                            const std::string &model_name,
                            json &out_prompt,
                            PromptType &out_pt,
                            int &out_ctx_size);

static int ParseContextSizeFromConfigJson(const std::string &config_json_path,
                                          const std::string &model_name,
                                          const std::string &log_prefix);

static void ParseBackendDeviceFromConfigJson(const std::string &config_json_path,
                                             const std::string &model_name,
                                             const std::string &log_prefix,
                                             std::string &out_backend,
                                             std::string &out_device);

class ModelManager::QNNImpl
{
    struct EmbeddingVerifier
    {
        QNNEmbeddingType embedding_type_{};
        ModelType model_type_{};
        struct EmbeddingFileSet
        {
            std::string serialized_file_;
            std::vector<std::string> bin_files_stack_;
            std::vector<std::string> tail_files_stack_;
        };
        std::unordered_map<ModelType, EmbeddingFileSet> embedding_file_set;

        QNNEmbedding CreateIfVerified() const
        {
            QNNEmbedding embedding;
            LibAppBuilder *app_builder;
            embedding.model_types_ = model_type_;

            for (auto &embedding_file: embedding_file_set)
            {
                auto &model_type = embedding_file.first;
                auto &files = embedding_file.second;
                QNNEmbedding::InferResource *infer_resource;
                if (!File::IsFileExist(files.serialized_file_) || File::IsFileEmpty(files.serialized_file_))
                {
                    My_Log{} << "veg file: " << files.serialized_file_ << " is invalid\n";
                    continue;
                }

                for (auto &bin_file: files.bin_files_stack_)
                {
                    if (!File::IsFileExist(bin_file) || File::IsFileEmpty(bin_file))
                    {
                        My_Log{} << "bin file: " << bin_file << " is invalid\n";
                        goto next_check;
                    }
                }

                for (auto &tail_file: files.tail_files_stack_)
                {
                    if (!File::IsFileExist(tail_file) || File::IsFileEmpty(tail_file))
                    {
                        My_Log{} << "tail_file: " << tail_file << " is invalid\n";
                        goto next_check;
                    }
                }

                app_builder = QNNEmbedding::LibAppbuilderCreator(files.serialized_file_, model_type.to_string());
                if (!app_builder)
                {
                    continue;
                }

                embedding.model_types_ |= model_type;
                embedding.infer_resources_.emplace(model_type, QNNEmbedding::InferResource{});
                infer_resource = &embedding.infer_resources_[model_type];
                infer_resource->bin_stacks_.reserve(files.bin_files_stack_.size());
                infer_resource->app_builder_ = app_builder;
                infer_resource->tag_ = model_type.to_string();
                for (const auto &bin_file: files.bin_files_stack_)
                {
                    infer_resource->bin_stacks_.emplace_back(File::ReadFile<uint8_t>(bin_file));
                }

                for (const auto &tail_file: files.tail_files_stack_)
                {
                    infer_resource->tails_bin_stacks_.emplace_back(File::ReadFile<uint8_t>(tail_file));
                }

                next_check:;
            }

            if (!embedding.infer_resources_.empty())
            {
                embedding.embedding_type_ = embedding_type_;
            }

            return embedding;
        };
    };

    struct PHI4Verifier : public EmbeddingVerifier
    {
        explicit PHI4Verifier(const std::string &model_path)
        {
            embedding_type_ = QNNEmbeddingType::PHI4MM;
            model_type_ = ModelType::Text;
            embedding_file_set.emplace(ModelType{ModelType::Vision}, EmbeddingFileSet{
                    model_path + "/veg.serialized.bin",
                    {},
                    {
                            model_path + "/raw/glb_gn.bin",
                            model_path + "/raw/sub_gn.bin"
                    }
            });
        }
    };

    struct Qwen2_5Verifier : public EmbeddingVerifier
    {
        explicit Qwen2_5Verifier(const std::string &model_path)
        {
            embedding_type_ = QNNEmbeddingType::QWEN2_5;
            model_type_ = ModelType::Text;
            embedding_file_set.emplace(ModelType{ModelType::Vision}, EmbeddingFileSet{
                    model_path + "/veg.serialized.bin",
                    {
                            model_path + "/raw/position_ids_cos.raw",
                            model_path + "/raw/position_ids_sin.raw",
                            model_path + "/raw/window_attention_mask.raw",
                            model_path + "/raw/full_attention_mask.raw"
                    }
            });
        }
    };

    struct Qwen2_5_OMINI_Verifier : public EmbeddingVerifier
    {
        explicit Qwen2_5_OMINI_Verifier(const std::string &model_path)
        {
            embedding_type_ = QNNEmbeddingType::QWEN2_5_OMINI;
            model_type_ = ModelType::Text;
            embedding_file_set.emplace(ModelType{ModelType::Audio},
                                       EmbeddingFileSet{
                                               model_path + "/qwen2.5_omini_audio/audio.serialized.bin",
                                               {}
                                       });

            embedding_file_set.emplace(ModelType{ModelType::Vision},
                                       EmbeddingFileSet{
                                               model_path + "/qwen2.5_omini_vision/veg.serialized.bin", {
                                                       model_path + "/qwen2.5_omini_vision/position_ids_cos.raw",
                                                       model_path + "/qwen2.5_omini_vision/position_ids_sin.raw",
                                                       model_path + "/qwen2.5_omini_vision/window_attention_mask.raw",
                                                       model_path + "/qwen2.5_omini_vision/full_attention_mask.raw"
                                               }
                                       });
        }
    };

    struct Qwen3VLVerifier : public EmbeddingVerifier
    {
        explicit Qwen3VLVerifier(const std::string &model_path)
        {
            embedding_type_ = QNNEmbeddingType::QWEN3_VL;
            model_type_ = ModelType::Text;
            embedding_file_set.emplace(ModelType{ModelType::Vision}, EmbeddingFileSet{
                    model_path + "/vision_encoder.bin",
                    {
                            model_path + "/sample_inputs/position_ids_cos.raw",
                            model_path + "/sample_inputs/position_ids_sin.raw",
                            model_path + "/sample_inputs/window_attention_mask.raw",
                            model_path + "/sample_inputs/full_attention_mask.raw"
                    }
            });
        }
    };

public:
    static QNNEmbedding TryCreate(const std::string &model_path, const std::string &raw_path)
    {
        struct Checker
        {
            QNNEmbeddingType embedding_type_;
            std::function<QNNEmbedding()> func_;
        };

        std::vector<Checker> checkers{
                {{QNNEmbeddingType::PHI4MM},        [model_path]() { return PHI4Verifier(model_path).CreateIfVerified(); }},
                {{QNNEmbeddingType::QWEN2_5},       [model_path]() { return Qwen2_5Verifier(model_path).CreateIfVerified(); }},
                {{QNNEmbeddingType::QWEN2_5_OMINI}, [model_path]() { return Qwen2_5_OMINI_Verifier(model_path).CreateIfVerified(); }},
                {{QNNEmbeddingType::QWEN3_VL},      [model_path]() { return Qwen3VLVerifier(model_path).CreateIfVerified(); }},
        };

        QNNEmbedding embedding;
        for (const auto &check: checkers)
        {
            My_Log{} << "try to check if qnn embedding is: "
                     << check.embedding_type_.to_string() << "\n";

            embedding = check.func_();
            if (embedding.embedding_type_ != QNNEmbeddingType::None)
            {
                embedding.embedded_raw_buf_ = File::ReadFile<uint8_t>(raw_path);
                return embedding;
            }
        }
        return {};
    }
};

struct ModelManager::ModeVerifier
{
    class ModeVerifierImpl
    {
    public:
        explicit ModeVerifierImpl(ModelInstanceConfig *config, ModelManager *manager)
                : config_{config}, self_{manager} {}

        virtual ~ModeVerifierImpl() = default;

        std::shared_ptr<ContextBase> CreateIfVerified()
        {
            bool matched = File::MatchFileInDir(config_->get_model_path(), ext_);
            if (!matched)
            {
                return nullptr;
            }

            if (!config_strict_)
            {
                return CreateIfVerifiedImpl();
            }

            std::string trimmed_cfg = config_->get_config_file();
            size_t cfg_start = trimmed_cfg.find_first_not_of(" \t\n\r");
            if (cfg_start != std::string::npos)
            {
                trimmed_cfg = trimmed_cfg.substr(cfg_start);
            }

            bool is_json_string = (!trimmed_cfg.empty() && trimmed_cfg[0] == '{');

            if (is_json_string)
            {
                return CreateIfVerifiedImpl();
            }

            if (File::IsFileExist(config_->get_config_file()) && !File::IsFileEmpty(config_->get_config_file()))
            {
                return CreateIfVerifiedImpl();
            }

            if (config_->get_known_model_path().empty())
            {
                std::string err_str{"config file is not found: " + config_->get_config_file()};
                My_Log{My_Log::Level::kWarning} << err_str << std::endl;
                return nullptr;
            }

            std::string new_config_path{File::ResolveModelConfigPath(config_->get_known_model_path())};
            My_Log{My_Log::Level::kError} << "config file: " << config_->get_config_file() << " "
                                          << "is not exist, will use default ver: " << new_config_path
                                          << std::endl;
            config_->set_config_file(new_config_path);

            return CreateIfVerifiedImpl();
        }

    protected:
        ModelInstanceConfig *config_;
        ModelManager *self_;
        bool config_strict_{true};
        const char *ext_{};

        void PublishSnapshot(const QNNEmbedding *embedding = nullptr)
        {
            config_->i_model_config_ = *self_;
            auto &snapshot = config_->i_model_config_;
            snapshot.genieModelHandle.reset();
            snapshot.model_path_ = config_->get_model_path();
            snapshot.config_file_ = config_->get_config_file();
            snapshot.known_model_path_ = config_->get_known_model_path();
            snapshot.model_name_ = config_->get_model_name();
            snapshot.prompt_ = config_->get_prompt_template();
            snapshot.prompt_type_ = config_->get_prompt_type();
            snapshot.thinking_model_ = config_->is_thinking_model();
            snapshot.qnn_embedding_ = embedding ? *embedding : QNNEmbedding{};
        }

        template<typename Ctx>
        std::shared_ptr<ContextBase> PublishContext()
        {
            PublishSnapshot();
            auto context = std::make_shared<Ctx>(*config_);
            config_->i_model_config_.genieModelHandle = context;
            return context;
        }

        enum class ConfigReadMode
        {
            kQnn,
            kGguf,
        };

        bool ReadConfigJson(json &j, ConfigReadMode mode) const
        {
            const std::string &src = config_->get_config_file();
            std::string trimmed = src;
            size_t start = trimmed.find_first_not_of(" \t\n\r");
            if (start != std::string::npos)
            {
                trimmed = trimmed.substr(start);
            }

            if (!trimmed.empty() && trimmed[0] == '{')
            {
                if (mode == ConfigReadMode::kQnn)
                {
                    try
                    {
                        j = json::parse(src);
                    } catch (const std::exception &)
                    {
                        return false;
                    }
                }
                else
                {
                    j = json::parse(src);
                }
                return true;
            }

            if (mode == ConfigReadMode::kQnn)
            {
                std::ifstream file(src);
                if (!file.good())
                {
                    return false;
                }
                file >> j;
            }
            else if (File::IsFileExist(src) && !File::IsFileEmpty(src))
            {
                std::ifstream file(src);
                file >> j;
            }
            else if (!File::IsFileExist(src))
            {
                std::ofstream(src).close();
            }
            return true;
        }

    private:
        virtual std::shared_ptr<ContextBase> CreateIfVerifiedImpl() = 0;
    };

    struct QnnVerifier : public ModeVerifierImpl
    {
        explicit QnnVerifier(ModelInstanceConfig *config, ModelManager *manager)
                : ModeVerifierImpl(config, manager) { ext_ = "bin"; }

        std::shared_ptr<ContextBase> CreateIfVerifiedImpl() override
        {
            json j;
            if (!ReadConfigJson(j, ConfigReadMode::kQnn))
            {
                return nullptr;
            }

            if (!j.contains("dialog"))
            {
                My_Log{My_Log::Level::kWarning} << "dialog is not exist in json object\n";
                return nullptr;
            }

            auto context_size = j["dialog"]["context"]["size"].get<int>();
            if (!context_size)
            {
                My_Log{My_Log::Level::kError} << "qnn config file is invalid\n";
            }
            else
            {
                config_->set_context_size(context_size);
                My_Log{} << "fixed qnn context size: " << config_->get_context_size() << "\n";
            }

            std::vector<std::string> files;
            std::string dtype_str;
            QNNEmbedding embedding;
            struct EmbeddingCleanup
            {
                QNNEmbedding &embedding;
                bool active{true};

                ~EmbeddingCleanup()
                {
                    if (active)
                        embedding.Clean();
                }
            } embedding_cleanup{embedding};

            if (!File::MatchFileInDir(config_->get_model_path(), "embedding_weights", &files))
            {
                goto ahead;
            }

            My_Log{} << "check qnn embedding file: " << files[0] << "\n";
            if (File::IsFileEmpty(files[0]))
            {
                throw std::runtime_error("embedded file: " + files[0] + " is invalid");
            }

            embedding = QNNImpl::TryCreate(config_->get_model_path(), files[0]);
            if (embedding.embedding_type_ == QNNEmbeddingType::None)
            {
                throw std::runtime_error("qnn model does not match any embedding rules");
            }
            My_Log{} << "check qnn embedding model type: " << embedding.model_types_.to_string() << "\n";

            if (!j.contains(json::json_pointer("/dialog/embedding/datatype")))
            {
                throw std::runtime_error("qnn embedding has bad config in datatype");
            }

            dtype_str = j.at(json::json_pointer("/dialog/embedding/datatype")).get_ref<const std::string &>();
            if (dtype_str == "float32")
            {
                embedding.data_type = EmbeddingDataType{EmbeddingDataType::FLOAT32};
            }
            else if (dtype_str == "ufixed8")
            {
                embedding.data_type = EmbeddingDataType{EmbeddingDataType::INT8};
            }

            if (embedding.data_type == EmbeddingDataType::None)
            {
                throw std::runtime_error("qnn embedding has bad datatype");
            }

            My_Log{} << "check qnn embedding data type: " << embedding.data_type.to_string() << "\n";
            ahead:
            config_->set_context_size(context_size);
            PublishSnapshot(&embedding);
            embedding_cleanup.active = false;
            auto context = std::make_shared<GenieContext>(*config_);
            config_->i_model_config_.genieModelHandle = context;
            return context;
        }
    };

#ifdef USE_MNN
    struct MnnVerifier : public ModeVerifierImpl
    {
        explicit MnnVerifier(ModelInstanceConfig *config, ModelManager *manager)
                : ModeVerifierImpl(config, manager) { ext_ = ".mnn"; }

        std::shared_ptr<ContextBase> CreateIfVerifiedImpl() override
        {
            uint64_t required = MNNContext::EstimateMnnMemoryRequirement(config_->get_model_path());
            uint64_t available_raw = MNNContext::GetAvailablePhysicalMemoryBytes();
            uint64_t other_models_reserved = self_->EstimateOtherLoadedModelsMemoryBytes(config_->get_model_name());
            bool available_unknown = (available_raw == UINT64_MAX);
            uint64_t available = available_unknown
                    ? UINT64_MAX
                    : (available_raw > other_models_reserved ? (available_raw - other_models_reserved) : 0);

            if (!available_unknown && required > available)
            {
                std::string detail = "insufficient memory to load MNN model: required=" +
                        std::to_string(required) + " bytes, available_raw=" + std::to_string(available_raw) +
                        " bytes, other_loaded_models_reserved=" + std::to_string(other_models_reserved) +
                        " bytes, effective_available=" + std::to_string(available) + " bytes";
                My_Log{My_Log::Level::kError} << "[MnnVerifier] " << detail << std::endl;
                self_->SetLastLoadFailureReason(ModelManager::LoadFailureReason::kInsufficientMemory, detail);
                return nullptr;
            }

            return PublishContext<MNNContext>();
        }
    };
#endif

    struct GGUFVerify : public ModeVerifierImpl
    {
        explicit GGUFVerify(ModelInstanceConfig *config, ModelManager *manager)
                : ModeVerifierImpl(config, manager)
        {
            config_strict_ = false;
            ext_ = ".gguf";
        }

        std::shared_ptr<ContextBase> CreateIfVerifiedImpl() override
        {
            auto load_with_device = [this](const std::string &device, bool draft_on_cpu = false,
                                           bool reduced_ubatch = false,
                                           bool full_gpu = false) -> std::shared_ptr<ContextBase>
            {
                My_Log{} << "[GGUFVerify] load attempt: device=" << device
                          << ", draft_device=" << (draft_on_cpu ? std::string("cpu") : device)
                          << ", reduced_ubatch=" << (reduced_ubatch ? 1 : 0)
                          << ", full_gpu=" << (full_gpu ? 1 : 0) << std::endl;
                config_->set_device(device);
                config_->set_speculative_draft_forced_cpu(draft_on_cpu);
                config_->set_speculative_reduced_ubatch(reduced_ubatch);
                config_->set_speculative_full_gpu(full_gpu);
                return PublishContext<LLAMACppBuilder>();
            };

            std::string requested_device = config_->get_device();
            std::transform(requested_device.begin(), requested_device.end(), requested_device.begin(),
                           [](unsigned char c) { return static_cast<char>(std::tolower(c)); });

            json j;
            try
            {
                ReadConfigJson(j, ConfigReadMode::kGguf);

                if (j.contains("backend") && j["backend"].is_string())
                {
                    std::string backend_field = j["backend"].get<std::string>();
                    std::transform(backend_field.begin(), backend_field.end(), backend_field.begin(),
                                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
                    if (backend_field == "cpu")
                    {
                        requested_device = "cpu";
                    }
                }
            }
            catch (...)
            {
                My_Log{My_Log::Level::kDebug}
                        << "[GGUFVerify] Failed to read optional backend field from model config.json, ignored and continue"
                        << std::endl;
            }

            llama_speculative::DraftModelConfig draft_config = llama_speculative::DraftModelConfig::ParseFrom(j);
            config_->set_speculative_draft_configured(draft_config.present);
            bool full_gpu_fits = false;
            if (draft_config.present)
            {

                uint64_t effective_context_size = llama_speculative::ResolveContextSizeByPhysicalMemory();
                config_->set_context_size(static_cast<int>(effective_context_size));
                uint64_t required = 0;
                uint64_t available = llama_speculative::GetAvailablePhysicalMemoryBytes();

                if (draft_config.planner == "off")
                {
                    uint64_t gpu_required = 0;
                    uint64_t gpu_available = 0;
                    full_gpu_fits = llama_speculative::SpeculativeFitsFullGpu(
                            config_->get_model_path(), draft_config.path, effective_context_size,
                            &gpu_required, &gpu_available);
                    My_Log{} << "[GGUFVerify] full GPU check: required=" << gpu_required
                              << " bytes, gpu_available=" << gpu_available
                              << " bytes, fits=" << (full_gpu_fits ? 1 : 0)
                              << " (planner off, legacy estimate)" << std::endl;
                    required = llama_speculative::EstimateSpeculativeMemoryRequirement(
                            config_->get_model_path(), draft_config.path, effective_context_size);
                }
                else
                {
                    const llama_runtime::RuntimePlan plan = llama_speculative::PlanSpeculativeRuntime(
                            config_->get_model_path(), draft_config, effective_context_size);
                    config_->set_runtime_plan(plan);
                    full_gpu_fits = plan.full_gpu;

                    const llama_runtime::MemoryLedger &led = plan.ledger;
                    My_Log{} << "[Planner] weights=" << (led.weights_tgt + led.weights_dft)
                              << " kv=" << (led.kv_attn_tgt + led.kv_attn_dft)
                              << " rs=" << (led.recurrent_tgt + led.recurrent_dft)
                              << " compute=" << (led.compute_const + led.compute_kq)
                              << " peak=" << led.runtime_peak
                              << " safety=" << led.safety
                              << " required=" << led.gpu_required()
                              << " free=" << plan.gpu_free_observed
                              << std::endl;
                    My_Log{} << "[Planner] " << plan.rationale << std::endl;
                    My_Log{} << "[GGUFVerify] full GPU check: required=" << led.gpu_required()
                              << " bytes, gpu_available=" << plan.gpu_free_observed
                              << " bytes, fits=" << (full_gpu_fits ? 1 : 0) << std::endl;
                    required = led.gpu_required() + led.host_checkpoints;

#ifdef USE_GGUF
                    const bool applied = LLAMACppBuilder::ApplyPlannedHeadCheckpoints(plan.head_checkpoints);
#else
                    const bool applied = false;
#endif
                    My_Log{} << "[Planner] head_checkpoints=" << plan.head_checkpoints
                              << (applied ? " (derived, applied)" : " (explicit config wins, not applied)")
                              << " ckpt_bytes=" << led.ckpt_bytes
                              << " host_resident=" << led.host_checkpoints
                              << " host_avail=" << plan.host_avail_observed << std::endl;
                }

                constexpr uint64_t kLoadPeakHeadroomBytes = 4ULL * 1024 * 1024 * 1024;
                if (available != UINT64_MAX && required + kLoadPeakHeadroomBytes > available)
                {
                    std::string detail = "insufficient available physical memory for speculative decoding "
                            "(draft_model) model: required=" + std::to_string(required) +
                            " bytes, available=" + std::to_string(available) +
                            " bytes, load_peak_headroom=" + std::to_string(kLoadPeakHeadroomBytes) +
                            " bytes, context_size=" + std::to_string(effective_context_size);
                    My_Log{My_Log::Level::kError} << "[GGUFVerify] " << detail << std::endl;
                    self_->SetLastLoadFailureReason(ModelManager::LoadFailureReason::kInsufficientMemory, detail);
                    return nullptr;
                }

                My_Log{} << "[GGUFVerify] draft_model declared: path=" << draft_config.path
                          << ", spec_type=" << draft_config.spec_type
                          << ", spec_draft_n_max=" << draft_config.spec_draft_n_max
                          << ", spec_draft_p_min=" << draft_config.spec_draft_p_min
                          << ", required=" << draft_config.required
                          << ", context_size=" << effective_context_size
                          << ", estimated_required_bytes=" << required
                          << ", available_physical_memory_bytes=" << available << std::endl;
            }

            bool target_weights_too_large = false;
            if (full_gpu_fits && requested_device != "cpu")
            {
                try
                {
                    return load_with_device("gpu", false, false, true);
                }
                catch (const std::exception &e)
                {
                    My_Log{My_Log::Level::kError}
                            << "[GGUFVerify] full GPU load failed, fallback to tuned split: " << e.what() << std::endl;
                }
                catch (...)
                {
                    My_Log{My_Log::Level::kError}
                            << "[GGUFVerify] full GPU load failed with unknown error, fallback to tuned split"
                            << std::endl;
                }
            }

            if (requested_device == "cpu")
            {
                return load_with_device("cpu");
            }

            try
            {
                return load_with_device("gpu");
            }
            catch (const TargetWeightsLoadError &e)
            {
                target_weights_too_large = true;
                My_Log{My_Log::Level::kError}
                        << "[GGUFVerify] GPU load failed on target weights, skipping remaining GPU tiers: "
                        << e.what() << std::endl;
            }
            catch (const std::exception &e)
            {
                My_Log{My_Log::Level::kError}
                        << "[GGUFVerify] GPU load failed, fallback to CPU: " << e.what() << std::endl;
            }
            catch (...)
            {
                My_Log{My_Log::Level::kError}
                        << "[GGUFVerify] GPU load failed with unknown error, fallback to CPU" << std::endl;
            }

            if (draft_config.present && !target_weights_too_large)
            {
                try
                {
                    return load_with_device("gpu", true);
                }
                catch (const std::exception &e)
                {
                    My_Log{My_Log::Level::kError}
                            << "[GGUFVerify] GPU load with draft on CPU failed, fallback to CPU: " << e.what()
                            << std::endl;
                }
                catch (...)
                {
                    My_Log{My_Log::Level::kError}
                            << "[GGUFVerify] GPU load with draft on CPU failed with unknown error, fallback to CPU"
                            << std::endl;
                }

                try
                {
                    return load_with_device("gpu", true, true);
                }
                catch (const std::exception &e)
                {
                    My_Log{My_Log::Level::kError}
                            << "[GGUFVerify] GPU load with draft on CPU and reduced ubatch failed, fallback to CPU: "
                            << e.what() << std::endl;
                }
                catch (...)
                {
                    My_Log{My_Log::Level::kError}
                            << "[GGUFVerify] GPU load with draft on CPU and reduced ubatch failed with unknown error, "
                               "fallback to CPU" << std::endl;
                }
            }

            try
            {
                return load_with_device("cpu");
            }
            catch (const std::exception &e)
            {
                My_Log{My_Log::Level::kError}
                        << "[GGUFVerify] CPU load failed after GPU fallback: " << e.what() << std::endl;
            }
            catch (...)
            {
                My_Log{My_Log::Level::kError}
                        << "[GGUFVerify] CPU load failed after GPU fallback with unknown error" << std::endl;
            }

            return nullptr;
        }
    };

    static std::shared_ptr<ContextBase> TryCreate(ModelInstanceConfig *config, ModelManager *manager)
    {
        struct Checker
        {
            ModelFormat model_format_;
            std::function<std::shared_ptr<ContextBase>()> func_;
        };

        std::vector<Checker> all_checkers{
                {{ModelFormat::QNN}, [config, manager](){ return QnnVerifier(config, manager).CreateIfVerified();}},
#ifdef USE_MNN
                {{ModelFormat::MNN}, [config, manager](){ return MnnVerifier(config, manager).CreateIfVerified();}},
#endif
#ifdef USE_GGUF
                {{ModelFormat::GGUF},[config, manager](){ return GGUFVerify(config, manager).CreateIfVerified();}}
#endif
        };

        std::vector<Checker> checkers;
        const std::string &specified_backend = config->get_backend();
        if (!specified_backend.empty() && specified_backend != "auto")
        {
            ModelFormat target_format{};
            bool format_found = false;
            if (specified_backend == "qnn" || specified_backend == "QNN")
            {
                target_format = ModelFormat::QNN;
                format_found = true;
            }
            else if (specified_backend == "mnn" || specified_backend == "MNN")
            {
                target_format = ModelFormat::MNN;
                format_found = true;
            }
            else if (specified_backend == "GGUF" || specified_backend == "gguf" ||
                     specified_backend == "llama" || specified_backend == "llama.cpp")
            {
                target_format = ModelFormat::GGUF;
                format_found = true;
            }

            if (format_found)
            {
                for (const auto &c: all_checkers)
                {
                    if (c.model_format_ == target_format)
                    {
                        checkers.push_back(c);
                        break;
                    }
                }
                if (checkers.empty())
                {
                    My_Log{My_Log::Level::kWarning}
                            << "[TryCreate] Specified backend '" << specified_backend
                            << "' is not compiled in. Falling back to auto-detection." << std::endl;
                    checkers = all_checkers;
                }
                else
                {
                    My_Log{} << "[TryCreate] Using specified backend: " << specified_backend << std::endl;
                }
            }
            else
            {
                My_Log{My_Log::Level::kWarning}
                        << "[TryCreate] Unknown backend '" << specified_backend
                        << "'. Falling back to auto-detection." << std::endl;
                checkers = all_checkers;
            }
        }
        else
        {
            checkers = all_checkers;
        }

        std::shared_ptr<ContextBase> context;

        try
        {
            for (const auto &check: checkers)
            {
                My_Log{} << "try to check if model is: "
                         << const_cast<ModelFormat &>(check.model_format_).to_string()
                         << " model\n";

                auto use_second = MeasureSeconds(
                        [&context, &check]()
                        {
                            context = check.func_();
                        }
                );

                if (context)
                {
                    My_Log{} << "load successfully! use second: " << use_second << " \n";
                    config->set_model_format(check.model_format_);
                    return context;
                }
            }
        }
        catch (std::exception &e)
        {
            My_Log{My_Log::Level::kError} << "create model context failed: " << e.what() << std::endl;
        }
        return nullptr;
    }
};

ModelManager::ModelManager(IModelConfig &&config) : IModelConfig{std::move(config)}
{
    if (log_level_ != -1)
        My_Log::Init(static_cast<My_Log::Level>(log_level_), "");
    My_Log{My_Log::Level::kAlways} << "setting log level by library: " << log_level_ << "\n";
}

ModelManager::~ModelManager()
{
    Clean();
}

bool ModelManager::LoadModelByName(const std::string &new_model, bool &first_load)
{
    if (new_model.empty())
    {
        My_Log{My_Log::Level::kError} << "model name can not be empty" << std::endl;
        return false;
    }

    if (GetModel(new_model) != nullptr)
    {
        My_Log{} << "model: " + new_model << " has already been loaded" << std::endl;
        first_load = false;
        return true;
    }

    std::lock_guard<std::recursive_mutex> load_lock(load_mutex_);
    if (GetModel(new_model) != nullptr)
    {
        first_load = false;
        return true;
    }
    first_load = true;

    My_Log{} << "model name: " + new_model << " will be loaded" << std::endl;
    UpdateModeList();
    std::vector<std::string> candidate_names;
    {
        std::lock_guard<std::mutex> list_lock(*model_list_mutex_);
        candidate_names = model_list_;
    }
    ModelLoadRequest request;
    for (const auto &name: candidate_names)
    {
        if (ModelComparer(name, new_model, false))
        {
            request.name = name;
            request.path = model_root_ + "/" + name;
            request.config_file = File::ResolveModelConfigPath(request.path);
            break;
        }
    }

    if (request.name.empty())
    {
        My_Log{My_Log::Level::kError} << "model name: " << new_model << " is not exist" << std::endl;
        return false;
    }

    return LoadModel(request);
}

static void ParsePromptSectionsConfig(const json &po, PromptSectionsConfig &sections_cfg, const std::string &tag)
{
    if (!po.contains(tag) || !po[tag].is_object())
        return;

    const auto &ps = po[tag];

    sections_cfg.enabled = ps.value("enabled", false);
    sections_cfg.default_action = ps.value("default_action", std::string("exclude"));
    sections_cfg.max_section_tokens = ps.value("max_section_tokens", 0);

    if (ps.contains("rules") && ps["rules"].is_array())
    {
        sections_cfg.rules.clear();
        for (const auto &rule_json: ps["rules"])
        {
            if (!rule_json.is_object())
                continue;
            PromptSectionRule rule;
            rule.title_contains = rule_json.value("title_contains", std::string(""));
            rule.heading_level = rule_json.value("heading_level", 0);
            rule.include = rule_json.value("include", true);
            if (!rule.title_contains.empty())
            {
                sections_cfg.rules.push_back(rule);
            }
        }
    }

    My_Log{} << "[Config] " << tag << " loaded: "
             << "enabled=" << sections_cfg.enabled
             << ", rules=" << sections_cfg.rules.size()
             << ", default_action=" << sections_cfg.default_action
             << ", max_section_tokens=" << sections_cfg.max_section_tokens
             << std::endl;
}

static void ParseRoutingConfig(const json &r, RoutingConfig &routing_config)
{
    routing_config.enabled = r.value("enabled", false);
    routing_config.policy_id = r.value("policy_id", std::string("default_v1"));
    routing_config.prefer_local_for_simple = r.value("prefer_local_for_simple", true);

    routing_config.enterprise_cloud_require_desensitize =
            r.value("enterprise_cloud_require_desensitize", false);

    if (r.contains("sensitivity_detection"))
    {
        const auto &sd = r["sensitivity_detection"];
        routing_config.sensitivity_detection.enabled = sd.value("enabled", true);
        routing_config.sensitivity_detection.method = sd.value("method", std::string("rule_first"));
        routing_config.sensitivity_detection.use_local_model_fallback = sd.value("use_local_model_fallback", false);
        routing_config.sensitivity_detection.strict_s2_union = sd.value("strict_s2_union", true);
        routing_config.sensitivity_detection.timeout_ms = sd.value("timeout_ms", 300000);
        routing_config.sensitivity_detection.model_input_max_chars = sd.value("model_input_max_chars", 2000);
        if (sd.contains("rule_level_overrides") && sd["rule_level_overrides"].is_object())
        {
            routing_config.sensitivity_detection.rule_level_overrides.clear();
            for (auto &[k, v]: sd["rule_level_overrides"].items())
            {
                if (v.is_string())
                    routing_config.sensitivity_detection.rule_level_overrides[k] = v.get<std::string>();
            }
        }

        {
            std::string kw_path = sd.value("keywords_dict_path", std::string(""));
            if (!kw_path.empty())
            {
                fs::path kw_fs_path(kw_path);
                if (kw_fs_path.is_relative())
                {

                    kw_path = (fs::path(RootDir) / kw_fs_path).generic_string();
                }
            }
            routing_config.sensitivity_detection.keywords_dict_path = kw_path;
        }
        routing_config.sensitivity_detection.keywords_reload_interval_seconds = sd.value("keywords_reload_interval_seconds", 60);

        routing_config.sensitivity_detection.max_gen_tokens = sd.value("max_gen_tokens", 2048);

        routing_config.sensitivity_detection.system_prompt = sd.value("system_prompt", std::string(""));

        routing_config.sensitivity_detection.debug_log_matches = sd.value("debug_log_matches", false);

        if (sd.contains("extended_rules") && sd["extended_rules"].is_object())
        {
            const auto &er = sd["extended_rules"];
            auto &xr = routing_config.sensitivity_detection.extended_rules;
            xr.enable_local_path = er.value("enable_local_path", true);
            xr.enable_internal_url = er.value("enable_internal_url", true);
            xr.enable_device_id = er.value("enable_device_id", true);
            xr.enable_image_data = er.value("enable_image_data", true);
            My_Log{} << "[Config] extended_rules loaded: "
                     << "local_path=" << xr.enable_local_path
                     << ", internal_url=" << xr.enable_internal_url
                     << ", device_id=" << xr.enable_device_id
                     << ", image_data=" << xr.enable_image_data
                     << std::endl;
        }

        if (sd.contains("detection_rules") && sd["detection_rules"].is_object())
        {
            const auto &dr = sd["detection_rules"];

            auto &rules = routing_config.sensitivity_detection.detection_rules;
            rules.enable_phone = dr.value("enable_phone", true);
            rules.enable_email = dr.value("enable_email", true);
            rules.enable_id_card = dr.value("enable_id_card", true);
            rules.enable_bank_card = dr.value("enable_bank_card", true);
            rules.enable_api_key = dr.value("enable_api_key", true);
            rules.enable_private_key = dr.value("enable_private_key", true);
            rules.enable_token = dr.value("enable_token", true);
            rules.enable_password = dr.value("enable_password", true);

            rules.level_phone = dr.value("level_phone", std::string("S1"));
            rules.level_email = dr.value("level_email", std::string("S1"));
            rules.level_id_card = dr.value("level_id_card", std::string("S2"));
            rules.level_bank_card = dr.value("level_bank_card", std::string("S2"));
            rules.level_api_key = dr.value("level_api_key", std::string("S2"));
            rules.level_private_key = dr.value("level_private_key", std::string("S2"));
            rules.level_token = dr.value("level_token", std::string("S2"));
            rules.level_password = dr.value("level_password", std::string("S1"));
            My_Log{} << "[Config] detection_rules loaded: "
                     << "phone=" << rules.enable_phone << "(level=" << rules.level_phone << ")"
                     << ", email=" << rules.enable_email << "(level=" << rules.level_email << ")"
                     << ", id_card=" << rules.enable_id_card << "(level=" << rules.level_id_card << ")"
                     << ", bank_card=" << rules.enable_bank_card << "(level=" << rules.level_bank_card << ")"
                     << ", api_key=" << rules.enable_api_key << "(level=" << rules.level_api_key << ")"
                     << ", private_key=" << rules.enable_private_key << "(level=" << rules.level_private_key << ")"
                     << ", token=" << rules.enable_token << "(level=" << rules.level_token << ")"
                     << ", password=" << rules.enable_password << "(level=" << rules.level_password << ")"
                     << std::endl;
        }
    }

    if (r.contains("desensitization"))
    {
        const auto &d = r["desensitization"];
        routing_config.desensitization.enabled = d.value("enabled", true);
        if (d.contains("strategies") && d["strategies"].is_array())
        {
            routing_config.desensitization.strategies.clear();
            for (const auto &s: d["strategies"])
                routing_config.desensitization.strategies.push_back(s.get<std::string>());
        }
        routing_config.desensitization.placeholder_style = d.value("placeholder_style", std::string("<{type}_{index}>"));
        routing_config.desensitization.iterative = d.value("iterative", false);
        routing_config.desensitization.max_rounds = d.value("max_rounds", 3);
        routing_config.desensitization.summarize_timeout_ms = d.value("summarize_timeout_ms", 300000);

        routing_config.desensitization.system_prompt = d.value("system_prompt", std::string(""));
        routing_config.desensitization.format_preserving_enabled = d.value("format_preserving_enabled", false);
        routing_config.desensitization.restore_response_enabled = d.value("restore_response_enabled", true);
        routing_config.desensitization.restore_stream_enabled = d.value("restore_stream_enabled", true);
        routing_config.desensitization.log_desensitization_details = d.value("log_desensitization_details", false);

        if (d.contains("entity_switches") && d["entity_switches"].is_object())
        {
            const auto &es = d["entity_switches"];
            routing_config.desensitization.entity_switches.enable_phone = es.value("enable_phone", true);
            routing_config.desensitization.entity_switches.enable_email = es.value("enable_email", true);
            routing_config.desensitization.entity_switches.enable_id_card = es.value("enable_id_card", true);
            routing_config.desensitization.entity_switches.enable_bank_card = es.value("enable_bank_card", true);
            routing_config.desensitization.entity_switches.enable_api_key = es.value("enable_api_key", true);
            routing_config.desensitization.entity_switches.enable_private_key = es.value("enable_private_key", true);
            routing_config.desensitization.entity_switches.enable_token = es.value("enable_token", true);
            routing_config.desensitization.entity_switches.enable_password = es.value("enable_password", true);
            routing_config.desensitization.entity_switches.enable_internal_url = es.value("enable_internal_url", true);
            routing_config.desensitization.entity_switches.enable_local_path = es.value("enable_local_path", true);
            routing_config.desensitization.entity_switches.enable_device_id = es.value("enable_device_id", true);
            routing_config.desensitization.entity_switches.enable_image_data = es.value("enable_image_data", true);
        }
    }

    if (r.contains("complexity"))
    {
        const auto &c = r["complexity"];
        routing_config.complexity.method = c.value("method", std::string("heuristic_first"));
        routing_config.complexity.use_local_model_fallback = c.value("use_local_model_fallback", false);
        routing_config.complexity.timeout_ms = c.value("timeout_ms", 300000);
        routing_config.complexity.model_input_max_chars = c.value("model_input_max_chars", 2000);

        routing_config.complexity.system_prompt = c.value("system_prompt", std::string(""));
        if (c.contains("thresholds"))
        {
            const auto &t = c["thresholds"];
            routing_config.complexity.thresholds.tool_calls = t.value("tool_calls", 3);
        }

        if (c.contains("keywords_c1") && c["keywords_c1"].is_array())
        {
            routing_config.complexity.keywords_c1.clear();
            for (const auto &kw: c["keywords_c1"])
            {
                if (kw.is_string())
                    routing_config.complexity.keywords_c1.push_back(kw.get<std::string>());
            }
        }
        if (c.contains("keywords_c2") && c["keywords_c2"].is_array())
        {
            routing_config.complexity.keywords_c2.clear();
            for (const auto &kw: c["keywords_c2"])
            {
                if (kw.is_string())
                    routing_config.complexity.keywords_c2.push_back(kw.get<std::string>());
            }
        }
    }

    if (r.contains("fallback"))
    {
        const auto &f = r["fallback"];
        routing_config.fallback.cloud_unavailable_to_local = f.value("cloud_unavailable_to_local", true);

        if (f.contains("local_unavailable") && f["local_unavailable"].is_object())
        {
            const auto &lu = f["local_unavailable"];
            routing_config.fallback.local_unavailable_s0 = lu.value("s0", std::string("cloud_if_allowed"));
            routing_config.fallback.local_unavailable_s1 = lu.value("s1", std::string("cloud_if_allowed"));
            routing_config.fallback.local_unavailable_s2 = lu.value("s2", std::string("fail"));
        }
        else
        {
            routing_config.fallback.local_unavailable_s0 = f.value("local_unavailable_s0", std::string("cloud_if_allowed"));
            routing_config.fallback.local_unavailable_s1 = f.value("local_unavailable_s1", std::string("cloud_if_allowed"));
            routing_config.fallback.local_unavailable_s2 = f.value("local_unavailable_s2", std::string("fail"));
        }
        routing_config.fallback.clean_local_history_on_fallback = f.value("clean_local_history_on_fallback", true);

        routing_config.fallback.max_input_overflow_retries = f.value("max_input_overflow_retries", 3);

        routing_config.fallback.enterprise_cloud_unavailable =
                f.value("enterprise_cloud_unavailable", std::string("public_cloud_if_allowed"));
        routing_config.fallback.public_cloud_unavailable =
                f.value("public_cloud_unavailable", std::string("enterprise_cloud_if_allowed"));
    }

    if (r.contains("cache"))
    {
        const auto &ca = r["cache"];
        routing_config.cache.ttl_seconds = ca.value("ttl_seconds", 60);
        routing_config.cache.max_entries = ca.value("max_entries", 256);
    }

    if (r.contains("agent_routing"))
    {
        const auto &ar = r["agent_routing"];
        routing_config.agent_routing.sub_agent_allow_cloud_on_c2 = ar.value("sub_agent_allow_cloud_on_c2", true);
    }

    if (r.contains("sticky_routing"))
    {
        const auto &sr = r["sticky_routing"];
        routing_config.sticky_routing.enabled = sr.value("enabled", false);
        routing_config.sticky_routing.ttl_seconds = sr.value("ttl_seconds", 1800);
        routing_config.sticky_routing.max_sessions = sr.value("max_sessions", 1000);
    }

    if (r.contains("metrics"))
    {
        const auto &m = r["metrics"];
        routing_config.metrics.summary_every_n_requests = m.value("summary_every_n_requests", 100);
        routing_config.metrics.summary_every_seconds = m.value("summary_every_seconds", 0);
        routing_config.metrics.latency_sample_size = m.value("latency_sample_size", 1000);
        routing_config.metrics.fail_reason_topn = m.value("fail_reason_topn", 5);
    }

    if (r.contains("incremental_check"))
    {
        const auto &ic = r["incremental_check"];
        routing_config.incremental_check.enabled = ic.value("enabled", false);
        routing_config.incremental_check.session_ttl_seconds = ic.value("session_ttl_seconds", 3600);
        routing_config.incremental_check.max_sessions = ic.value("max_sessions", 1000);
        routing_config.incremental_check.s2_always_full_check = ic.value("s2_always_full_check", true);
        routing_config.incremental_check.detect_sensitive_reference = ic.value("detect_sensitive_reference", true);
        routing_config.incremental_check.detect_history_tampering = ic.value("detect_history_tampering", true);
        My_Log{} << "Incremental check config loaded: enabled="
                 << routing_config.incremental_check.enabled
                 << ", session_ttl_seconds=" << routing_config.incremental_check.session_ttl_seconds
                 << ", max_sessions=" << routing_config.incremental_check.max_sessions << std::endl;
    }

    if (r.contains("s2_turn_cleaning"))
    {
        const auto &stc = r["s2_turn_cleaning"];
        routing_config.s2_turn_cleaning.enabled = stc.value("enabled", true);
        routing_config.s2_turn_cleaning.log_details = stc.value("log_details", true);
        routing_config.s2_turn_cleaning.allow_cloud_reroute_after_clean = stc.value("allow_cloud_reroute_after_clean", false);
        My_Log{} << "S2 turn cleaning config loaded: enabled="
                 << routing_config.s2_turn_cleaning.enabled
                 << ", log_details=" << routing_config.s2_turn_cleaning.log_details
                 << ", allow_cloud_reroute="
                 << routing_config.s2_turn_cleaning.allow_cloud_reroute_after_clean << std::endl;
    }

    My_Log{} << "Routing config loaded: enabled=" << routing_config.enabled
             << ", policy_id=" << routing_config.policy_id << std::endl;
}

namespace
{
struct SharedCloudSettings
{
    int timeout_seconds = 120;
    int stream_timeout_seconds = 600;
    bool log_debug = false;
    int retry_max = 2;
    int retry_backoff_ms = 200;
    int retry_max_total_attempts = 0;
    bool retry_on_429_switch_endpoint = false;
    int cb_failure_threshold = 3;
    int cb_cooldown_seconds = 60;
    int rl_max_inferences = 20;
    int rl_max_tokens = 0;
};
}

static void ParseSharedCloudSettings(const json &cs, SharedCloudSettings &s)
{
    s.timeout_seconds = cs.value("timeout_seconds", s.timeout_seconds);
    s.stream_timeout_seconds = cs.value("stream_timeout_seconds", s.stream_timeout_seconds);
    s.log_debug = cs.value("log_debug", s.log_debug);
    if (cs.contains("retry"))
    {
        const auto &ret = cs["retry"];
        s.retry_max = ret.value("max", s.retry_max);
        s.retry_backoff_ms = ret.value("backoff_ms", s.retry_backoff_ms);
        s.retry_max_total_attempts = ret.value("max_total_attempts", s.retry_max_total_attempts);
        s.retry_on_429_switch_endpoint = ret.value("retry_on_429_switch_endpoint", s.retry_on_429_switch_endpoint);
    }
    if (cs.contains("circuit_breaker"))
    {
        const auto &cb = cs["circuit_breaker"];
        s.cb_failure_threshold = cb.value("failure_threshold", s.cb_failure_threshold);
        s.cb_cooldown_seconds = cb.value("cooldown_seconds", s.cb_cooldown_seconds);
    }
    if (cs.contains("rate_limit"))
    {
        const auto &rl = cs["rate_limit"];
        s.rl_max_inferences = rl.value("max_inferences_per_task", s.rl_max_inferences);
        s.rl_max_tokens = rl.value("max_tokens_per_task", s.rl_max_tokens);
    }
}

template <typename CloudCfg>
static void ApplyCloudSharedFields(const json &j, CloudCfg &cfg, const SharedCloudSettings &s)
{
    cfg.timeout_seconds = j.value("timeout_seconds", s.timeout_seconds);
    cfg.stream_timeout_seconds = j.value("stream_timeout_seconds", s.stream_timeout_seconds);
    cfg.log_debug = j.value("log_debug", s.log_debug);

    if (j.contains("retry"))
    {
        const auto &ret = j["retry"];
        cfg.retry.max = ret.value("max", s.retry_max);
        cfg.retry.backoff_ms = ret.value("backoff_ms", s.retry_backoff_ms);
        cfg.retry.max_total_attempts = ret.value("max_total_attempts", s.retry_max_total_attempts);
        cfg.retry.retry_on_429_switch_endpoint = ret.value("retry_on_429_switch_endpoint", s.retry_on_429_switch_endpoint);
    }
    else
    {
        cfg.retry.max = s.retry_max;
        cfg.retry.backoff_ms = s.retry_backoff_ms;
        cfg.retry.max_total_attempts = s.retry_max_total_attempts;
        cfg.retry.retry_on_429_switch_endpoint = s.retry_on_429_switch_endpoint;
    }

    if (j.contains("circuit_breaker"))
    {
        const auto &cb = j["circuit_breaker"];
        cfg.circuit_breaker.failure_threshold = cb.value("failure_threshold", s.cb_failure_threshold);
        cfg.circuit_breaker.cooldown_seconds = cb.value("cooldown_seconds", s.cb_cooldown_seconds);
    }
    else
    {
        cfg.circuit_breaker.failure_threshold = s.cb_failure_threshold;
        cfg.circuit_breaker.cooldown_seconds = s.cb_cooldown_seconds;
    }

    if (j.contains("endpoints") && j["endpoints"].is_array())
    {
        cfg.endpoints.clear();
        for (const auto &ep: j["endpoints"])
        {
            typename CloudCfg::Endpoint endpoint;
            endpoint.name = ep.value("name", std::string(""));
            endpoint.base_url = ep.value("base_url", std::string(""));
            endpoint.model = ep.value("model", std::string(""));
            cfg.endpoints.push_back(endpoint);
        }
    }

    if (j.contains("rate_limit"))
    {
        const auto &rl = j["rate_limit"];
        cfg.rate_limit.max_inferences_per_task = rl.value("max_inferences_per_task", s.rl_max_inferences);
        cfg.rate_limit.max_tokens_per_task = rl.value("max_tokens_per_task", s.rl_max_tokens);
    }
    else
    {
        cfg.rate_limit.max_inferences_per_task = s.rl_max_inferences;
        cfg.rate_limit.max_tokens_per_task = s.rl_max_tokens;
    }
}

bool ModelManager::InitializeConfig()
{
    fs::path config_path{config_file_};
    My_Log{} << "ModelManager::LoadModel,configFile=" + config_path.generic_string() << std::endl;

    auto ensure_path{
            [](const fs::path &path)
            {
                auto str = path.generic_string();
                if (str.empty())
                    throw std::runtime_error(
                            "the model file layout does not meet the standard, "
                            "it must be /models/{model_name}/{config}");
                return str;
            }
    };

    try
    {
        model_path_ = ensure_path(config_path.parent_path().generic_string());
        model_name_ = config_path.parent_path().filename().generic_string();
        model_root_ = ensure_path(config_path.parent_path().parent_path().generic_string());
    }
    catch (std::exception &e)
    {
        My_Log{My_Log::Level::kError} << e.what() << std::endl;
        return false;
    }

    {
        fs::path prompt_engineering_path = fs::path(RootDir) / "config" / "prompt_engineering.json";
        if (File::IsFileExist(prompt_engineering_path.generic_string()) &&
            !File::IsFileEmpty(prompt_engineering_path.generic_string()))
        {
            try
            {
                std::ifstream pe_file(prompt_engineering_path.generic_string());
                json pe_json;
                pe_file >> pe_json;

                if (pe_json.contains("debug"))
                {
                    const auto &dbg = pe_json["debug"];
                    ResponseTools::status_content_visible = dbg.value("status_update_content_visible", true);
                    ResponseTools::log_inference_stream = dbg.value("log_inference_stream", false);
                    My_Log{} << "Debug config loaded: status_update_content_visible="
                             << ResponseTools::status_content_visible
                             << ", log_inference_stream=" << ResponseTools::log_inference_stream
                             << std::endl;
                }

                if (pe_json.contains("windows_shell_hint") && pe_json["windows_shell_hint"].is_object())
                {
                    const auto &wsh = pe_json["windows_shell_hint"];
                    windows_shell_hint_config_.enabled = wsh.value("enabled", true);
                    windows_shell_hint_config_.hint_text = wsh.value("hint_text", windows_shell_hint_config_.hint_text);
                    if (wsh.contains("tool_keywords") && wsh["tool_keywords"].is_array())
                    {
                        windows_shell_hint_config_.tool_keywords.clear();
                        for (const auto &kw : wsh["tool_keywords"])
                        {
                            if (kw.is_string())
                            {
                                windows_shell_hint_config_.tool_keywords.push_back(kw.get<std::string>());
                            }
                        }
                    }
                    My_Log{} << "[Config] windows_shell_hint loaded: enabled="
                             << windows_shell_hint_config_.enabled
                             << ", tool_keywords_count=" << windows_shell_hint_config_.tool_keywords.size()
                             << std::endl;
                }

                if (pe_json.contains("adaptive_thinking") && pe_json["adaptive_thinking"].is_object())
                {
                    const auto &at = pe_json["adaptive_thinking"];
                    auto &at_cfg = adaptive_thinking_config_;
                    at_cfg.enabled = at.value("enabled", at_cfg.enabled);
                    at_cfg.time_budget_s = at.value("time_budget_s", at_cfg.time_budget_s);
                    at_cfg.min_tokens = at.value("min_tokens", at_cfg.min_tokens);
                    at_cfg.max_per_signature = at.value("max_per_signature", at_cfg.max_per_signature);
                    at_cfg.max_think_turn_share = at.value("max_think_turn_share", at_cfg.max_think_turn_share);
                    at_cfg.error_repeat_window = at.value("error_repeat_window", at_cfg.error_repeat_window);
                    at_cfg.near_repeat_window = at.value("near_repeat_window", at_cfg.near_repeat_window);
                    at_cfg.max_tokens = at.value("max_tokens", at_cfg.max_tokens);
                    at_cfg.ctx_reserve_tokens = at.value("ctx_reserve_tokens", at_cfg.ctx_reserve_tokens);
                    at_cfg.goal_reminder_chars = at.value("goal_reminder_chars", at_cfg.goal_reminder_chars);
                    at_cfg.max_consecutive_block = at.value("max_consecutive_block", at_cfg.max_consecutive_block);
                    at_cfg.reserved_failure_think_turns =
                            at.value("reserved_failure_think_turns", at_cfg.reserved_failure_think_turns);
                    My_Log{} << "[Config] adaptive_thinking loaded: enabled=" << at_cfg.enabled
                             << ", time_budget_s=" << at_cfg.time_budget_s
                             << ", max_tokens=" << at_cfg.max_tokens << std::endl;
                }

                if (pe_json.contains("tool_call_repair") && pe_json["tool_call_repair"].is_object())
                {
                    const auto &tcr = pe_json["tool_call_repair"];
                    tool_call_repair_config_.enabled = tcr.value("enabled", true);

                    if (tcr.contains("internal_retry") && tcr["internal_retry"].is_object())
                    {
                        const auto &ir = tcr["internal_retry"];
                        auto &ir_cfg = tool_call_repair_config_.internal_retry;
                        ir_cfg.max_attempts = ir.value("max_attempts", 1);
                        if (ir.contains("skip_reasons") && ir["skip_reasons"].is_array())
                        {
                            ir_cfg.skip_reasons.clear();
                            for (const auto &reason : ir["skip_reasons"])
                            {
                                if (reason.is_string())
                                {
                                    ir_cfg.skip_reasons.push_back(reason.get<std::string>());
                                }
                            }
                        }
                    }

                    if (tcr.contains("circuit_breaker") && tcr["circuit_breaker"].is_object())
                    {
                        const auto &cb = tcr["circuit_breaker"];
                        auto &cb_cfg = tool_call_repair_config_.circuit_breaker;
                        cb_cfg.consecutive_layer3_threshold = cb.value("consecutive_layer3_threshold", 3);
                    }
                    ToolCallCircuitBreakerStore::GetInstance().Configure(tool_call_repair_config_.circuit_breaker);

                    if (tcr.contains("redundant_call_guard") && tcr["redundant_call_guard"].is_object())
                    {
                        const auto &rg = tcr["redundant_call_guard"];
                        auto &rg_cfg = tool_call_repair_config_.redundant_call_guard;
                        rg_cfg.enabled = rg.value("enabled", true);
                        rg_cfg.repeat_threshold = rg.value("repeat_threshold", 2);
                        rg_cfg.consecutive_failure_threshold = rg.value("consecutive_failure_threshold", 3);
                        rg_cfg.non_contiguous_repeat_threshold = rg.value("non_contiguous_repeat_threshold", 2);
                        rg_cfg.internal_retry_threshold = rg.value("internal_retry_threshold", 2);
                        rg_cfg.hard_block_threshold = rg.value("hard_block_threshold", 3);
                        rg_cfg.hard_block_stop_after = rg.value("hard_block_stop_after", 2);
                        rg_cfg.soft_retry_enabled = rg.value("soft_retry_enabled", false);
                        rg_cfg.max_signatures_per_entry = rg.value("max_signatures_per_entry", 256);
                        rg_cfg.note_min_interval_turns = rg.value("note_min_interval_turns", 4);
                        rg_cfg.overlap_reread_threshold = rg.value("overlap_reread_threshold", 2);
                    }
                    ToolCallRepetitionStore::GetInstance().Configure(tool_call_repair_config_.redundant_call_guard);

                    My_Log{} << "[Config] tool_call_repair loaded: enabled="
                             << tool_call_repair_config_.enabled
                             << ", internal_retry.max_attempts="
                             << tool_call_repair_config_.internal_retry.max_attempts
                             << ", internal_retry.skip_reasons=["
                             << [&]() {
                                    std::string joined;
                                    for (const auto &r : tool_call_repair_config_.internal_retry.skip_reasons)
                                    {
                                        if (!joined.empty()) joined += ",";
                                        joined += r;
                                    }
                                    return joined;
                                }()
                             << "], circuit_breaker.consecutive_layer3_threshold="
                             << tool_call_repair_config_.circuit_breaker.consecutive_layer3_threshold
                             << ", redundant_call_guard.internal_retry_threshold="
                             << tool_call_repair_config_.redundant_call_guard.internal_retry_threshold
                             << ", redundant_call_guard.hard_block_threshold="
                             << tool_call_repair_config_.redundant_call_guard.hard_block_threshold
                             << ", redundant_call_guard.soft_retry_enabled="
                             << tool_call_repair_config_.redundant_call_guard.soft_retry_enabled
                             << std::endl;
                }

                if (pe_json.contains("debug") && pe_json["debug"].is_object())
                {
                    const auto &dbg = pe_json["debug"];
                    if (dbg.contains("request_trace") && dbg["request_trace"].is_object())
                    {
                        const auto &rt = dbg["request_trace"];
                        request_trace_config_.enabled = rt.value("enabled", false);
                        request_trace_config_.path = rt.value("path", std::string("tmp/request_trace.jsonl"));
                        My_Log{} << "[Config] debug.request_trace loaded: enabled="
                                 << request_trace_config_.enabled
                                 << ", path=" << request_trace_config_.path
                                 << std::endl;
                    }
                }
                RequestTraceWriter::GetInstance().Configure(request_trace_config_);

#ifdef USE_GGUF
                auto &grg_cfg = generation_repeat_guard_config_;
                if (pe_json.contains("generation_repeat_guard") && pe_json["generation_repeat_guard"].is_object())
                {
                    const auto &grg = pe_json["generation_repeat_guard"];
                    grg_cfg.enabled = grg.value("enabled", grg_cfg.enabled);
                    grg_cfg.max_period_tokens = grg.value("max_period_tokens", grg_cfg.max_period_tokens);
                }
                LLAMACppBuilder::SetDegenerateRepeatGuardEnabled(grg_cfg.enabled);
                LLAMACppBuilder::SetDegenerateRepeatGuardMaxPeriod(grg_cfg.max_period_tokens);
                My_Log{} << "[Config] generation_repeat_guard loaded: enabled=" << grg_cfg.enabled
                         << ", max_period_tokens=" << grg_cfg.max_period_tokens << std::endl;
                {
                    bool prefix_cache_enabled = true;
                    int head_checkpoints = 2;
                    bool head_checkpoints_explicit = false;
                    if (pe_json.contains("llama_prefix_cache") && pe_json["llama_prefix_cache"].is_object())
                    {
                        prefix_cache_enabled = pe_json["llama_prefix_cache"].value("enabled", true);
                        head_checkpoints = pe_json["llama_prefix_cache"].value("head_checkpoints", 2);
                        head_checkpoints_explicit = pe_json["llama_prefix_cache"].contains("head_checkpoints");
                    }
                    LLAMACppBuilder::SetPrefixCacheEnabled(prefix_cache_enabled);
                    LLAMACppBuilder::SetPrefixHeadCheckpoints(head_checkpoints, head_checkpoints_explicit);
                    My_Log{} << "[Config] llama_prefix_cache.enabled=" << prefix_cache_enabled
                             << " head_checkpoints=" << head_checkpoints
                             << (head_checkpoints_explicit ? " (explicit)" : " (default)") << std::endl;
                }
#endif

                if (pe_json.contains("prompt_optimization"))
                {
                    const auto &po = pe_json["prompt_optimization"];
                    prompt_optimization_config_.output_reserve_ratio = po.value("output_reserve_ratio", 0.20f);
                    prompt_optimization_config_.max_messages_limit = po.value("max_messages_limit", (size_t) 16);
                    prompt_optimization_config_.token_pressure_trigger_ratio =
                            po.value("token_pressure_trigger_ratio", 0.75);
                    prompt_optimization_config_.recent_window = po.value("recent_window", (size_t) 6);
                    prompt_optimization_config_.old_compress_len = po.value("old_compress_len", (size_t) 300);
                    prompt_optimization_config_.recent_compress_len = po.value("recent_compress_len", (size_t) 600);
                    prompt_optimization_config_.tool_compress_len = po.value("tool_compress_len", (size_t) 400);
                    prompt_optimization_config_.min_compress_threshold = po.value("min_compress_threshold", (size_t) 10);
                    prompt_optimization_config_.tool_min_length = po.value("tool_min_length", (size_t) 300);

                    prompt_optimization_config_.skill_catalog_format = po.value("skill_catalog_format", "structured");
                    prompt_optimization_config_.enable_skill_auto_correction = po.value("enable_skill_auto_correction", true);

                    if (po.contains("emergency_truncation") && po["emergency_truncation"].is_object())
                    {
                        const auto &et = po["emergency_truncation"];
                        prompt_optimization_config_.emergency_truncation.enabled =
                                et.value("enabled", true);
                        prompt_optimization_config_.emergency_truncation.max_truncation_ratio =
                                et.value("max_truncation_ratio", 0.95f);
                        prompt_optimization_config_.emergency_truncation.safety_margin_tokens =
                                et.value("safety_margin_tokens", 30);
                        prompt_optimization_config_.emergency_truncation.target_largest_tool_messages =
                                et.value("target_largest_tool_messages", true);
                        My_Log{} << "[Config] emergency_truncation loaded: "
                                 << "enabled=" << prompt_optimization_config_.emergency_truncation.enabled
                                 << ", max_truncation_ratio="
                                 << prompt_optimization_config_.emergency_truncation.max_truncation_ratio
                                 << ", safety_margin_tokens="
                                 << prompt_optimization_config_.emergency_truncation.safety_margin_tokens
                                 << ", target_largest_tool_messages="
                                 << prompt_optimization_config_.emergency_truncation.target_largest_tool_messages
                                 << std::endl;
                    }

                    if (po.contains("fidelity") && po["fidelity"].is_object())
                    {
                        const auto &fid = po["fidelity"];
                        auto &fid_cfg = prompt_optimization_config_.fidelity;
                        fid_cfg.budget_unit = fid.value("budget_unit", std::string("tokens"));
                        fid_cfg.preserve_tail = fid.value("preserve_tail", true);
                        fid_cfg.tail_ratio = fid.value("tail_ratio", 0.30);
                        fid_cfg.extract_high_signal = fid.value("extract_high_signal", true);
                        fid_cfg.drop_placeholder = fid.value("drop_placeholder", true);
                        fid_cfg.json_head_items = fid.value("json_head_items", (size_t) 3);
                        fid_cfg.json_tail_items = fid.value("json_tail_items", (size_t) 1);
                        fid_cfg.max_token_probe_per_message = fid.value("max_token_probe_per_message", (size_t) 8);
                        fid_cfg.cjk_bytes_per_token = fid.value("cjk_bytes_per_token", 3.0);
                        fid_cfg.ascii_bytes_per_token = fid.value("ascii_bytes_per_token", 4.0);
                        My_Log{} << "[Config] fidelity loaded: "
                                 << "budget_unit=" << fid_cfg.budget_unit
                                 << ", preserve_tail=" << fid_cfg.preserve_tail
                                 << ", tail_ratio=" << fid_cfg.tail_ratio
                                 << ", extract_high_signal=" << fid_cfg.extract_high_signal
                                 << ", drop_placeholder=" << fid_cfg.drop_placeholder
                                 << ", json_head_items=" << fid_cfg.json_head_items
                                 << ", json_tail_items=" << fid_cfg.json_tail_items
                                 << ", max_token_probe_per_message=" << fid_cfg.max_token_probe_per_message
                                 << ", cjk_bytes_per_token=" << fid_cfg.cjk_bytes_per_token
                                 << ", ascii_bytes_per_token=" << fid_cfg.ascii_bytes_per_token
                                 << std::endl;
                    }

                    if (po.contains("budget_partition") && po["budget_partition"].is_object())
                    {
                        const auto &bp = po["budget_partition"];
                        auto &bp_cfg = prompt_optimization_config_.budget_partition;
                        bp_cfg.enabled = bp.value("enabled", true);
                        bp_cfg.skills_floor_ratio = bp.value("skills_floor_ratio", 0.15);
                        bp_cfg.tools_ratio = bp.value("tools_ratio", 0.35);
                        My_Log{} << "[Config] budget_partition loaded: "
                                 << "enabled=" << bp_cfg.enabled
                                 << ", skills_floor_ratio=" << bp_cfg.skills_floor_ratio
                                 << ", tools_ratio=" << bp_cfg.tools_ratio
                                 << std::endl;
                    }

                    if (po.contains("relevance_filter") && po["relevance_filter"].is_object())
                    {
                        const auto &rf = po["relevance_filter"];
                        prompt_optimization_config_.relevance_filter.enabled =
                                rf.value("enabled", true);
                        prompt_optimization_config_.relevance_filter.name_token_weight =
                                rf.value("name_token_weight", (size_t) 3);
                        prompt_optimization_config_.relevance_filter.description_keyword_weight =
                                rf.value("description_keyword_weight", (size_t) 1);
                        prompt_optimization_config_.relevance_filter.zero_hit_keep_all =
                                rf.value("zero_hit_keep_all", true);
                        prompt_optimization_config_.relevance_filter.cjk_bigram =
                                rf.value("cjk_bigram", true);
                        prompt_optimization_config_.relevance_filter.intent_aliases_enabled =
                                rf.value("intent_aliases_enabled", true);
                        prompt_optimization_config_.relevance_filter.tag_weight =
                                rf.value("tag_weight", (size_t) 2);
                        prompt_optimization_config_.relevance_filter.anchor_first_user_message =
                                rf.value("anchor_first_user_message", true);
                        prompt_optimization_config_.relevance_filter.keep_all_tools_ratio =
                                rf.value("keep_all_tools_ratio", 0.15);
                        prompt_optimization_config_.relevance_filter.protect_core_capability_tools =
                                rf.value("protect_core_capability_tools", true);
                        if (rf.contains("core_capability_keywords") && rf["core_capability_keywords"].is_array())
                        {
                            prompt_optimization_config_.relevance_filter.core_capability_keywords.clear();
                            for (const auto &kw : rf["core_capability_keywords"])
                            {
                                if (kw.is_string())
                                {
                                    prompt_optimization_config_.relevance_filter.core_capability_keywords
                                            .push_back(kw.get<std::string>());
                                }
                            }
                        }
                        My_Log{} << "[Config] relevance_filter loaded: "
                                 << "enabled=" << prompt_optimization_config_.relevance_filter.enabled
                                 << ", name_token_weight=" << prompt_optimization_config_.relevance_filter.name_token_weight
                                 << ", description_keyword_weight=" << prompt_optimization_config_.relevance_filter.description_keyword_weight
                                 << ", zero_hit_keep_all=" << prompt_optimization_config_.relevance_filter.zero_hit_keep_all
                                 << ", cjk_bigram=" << prompt_optimization_config_.relevance_filter.cjk_bigram
                                 << ", intent_aliases_enabled=" << prompt_optimization_config_.relevance_filter.intent_aliases_enabled
                                 << ", tag_weight=" << prompt_optimization_config_.relevance_filter.tag_weight
                                 << ", anchor_first_user_message=" << prompt_optimization_config_.relevance_filter.anchor_first_user_message
                                 << ", protect_core_capability_tools=" << prompt_optimization_config_.relevance_filter.protect_core_capability_tools
                                 << ", core_capability_keywords_count=" << prompt_optimization_config_.relevance_filter.core_capability_keywords.size()
                                 << std::endl;
                    }

                    if (po.contains("skill_disclosure") && po["skill_disclosure"].is_object())
                    {
                        const auto &sd2 = po["skill_disclosure"];
                        auto &sd_cfg = prompt_optimization_config_.skill_disclosure;
                        sd_cfg.enabled = sd2.value("enabled", true);
                        sd_cfg.l2_top_k = sd2.value("l2_top_k", (size_t) 2);
                        sd_cfg.l1_top_k = sd2.value("l1_top_k", (size_t) 6);
                        sd_cfg.l1_summary_max_chars = sd2.value("l1_summary_max_chars", (size_t) 240);
                        sd_cfg.l0_summary_max_chars = sd2.value("l0_summary_max_chars", (size_t) 80);

                        sd_cfg.tie_aware_l2 = sd2.value("tie_aware_l2", sd_cfg.tie_aware_l2);
                        My_Log{} << "[Config] skill_disclosure loaded: "
                                 << "enabled=" << sd_cfg.enabled
                                 << ", l2_top_k=" << sd_cfg.l2_top_k
                                 << ", l1_top_k=" << sd_cfg.l1_top_k
                                 << ", l1_summary_max_chars=" << sd_cfg.l1_summary_max_chars
                                 << ", l0_summary_max_chars=" << sd_cfg.l0_summary_max_chars
                                 << ", tie_aware_l2=" << sd_cfg.tie_aware_l2
                                 << std::endl;
                    }

                    if (po.contains("long_text_summarization") && po["long_text_summarization"].is_object())
                    {
                        const auto &lts = po["long_text_summarization"];
                        auto &sum_cfg = prompt_optimization_config_.long_text_summarization;

                        sum_cfg.enabled = lts.value("enabled", false);
                        sum_cfg.trigger_ratio = lts.value("trigger_ratio", 0.5);
                        sum_cfg.chunk_ratio = lts.value("chunk_ratio", 0.45);
                        sum_cfg.summarize_user_messages = lts.value("summarize_user_messages", true);
                        sum_cfg.summarize_tool_responses = lts.value("summarize_tool_responses", true);
                        sum_cfg.max_chunks = lts.value("max_chunks", 4);
                        sum_cfg.verbose_logging = lts.value("verbose_logging", false);
                        sum_cfg.map_instruction = lts.value("map_instruction", sum_cfg.map_instruction);
                        sum_cfg.reduce_instruction = lts.value("reduce_instruction", sum_cfg.reduce_instruction);

                        if (lts.contains("cache") && lts["cache"].is_object())
                        {
                            const auto &ca = lts["cache"];
                            sum_cfg.cache.enabled = ca.value("enabled", true);
                            sum_cfg.cache.max_entries = ca.value("max_entries", (size_t) 500);
                            sum_cfg.cache.max_memory_mb = ca.value("max_memory_mb", (size_t) 50);
                            sum_cfg.cache.ttl_minutes = ca.value("ttl_minutes", 60);
                        }

                        My_Log{My_Log::Level::kInfo}
                                << "[LongTextSummarization] Config loaded: enabled=" << sum_cfg.enabled
                                << ", trigger_ratio=" << sum_cfg.trigger_ratio
                                << ", chunk_ratio=" << sum_cfg.chunk_ratio
                                << ", max_chunks=" << sum_cfg.max_chunks
                                << ", summarize_user=" << sum_cfg.summarize_user_messages
                                << ", summarize_tool=" << sum_cfg.summarize_tool_responses
                                << ", cache.enabled=" << sum_cfg.cache.enabled
                                << ", cache.max_entries=" << sum_cfg.cache.max_entries
                                << ", cache.ttl_minutes=" << sum_cfg.cache.ttl_minutes
                                << std::endl;

                        if (sum_cfg.enabled && sum_cfg.cache.enabled)
                        {
                            SummaryCache::GetInstance().Configure(sum_cfg.cache);
                            My_Log{My_Log::Level::kInfo}
                                    << "[LongTextSummarization] SummaryCache configured: max_entries="
                                    << sum_cfg.cache.max_entries
                                    << ", max_memory_mb=" << sum_cfg.cache.max_memory_mb
                                    << ", ttl_minutes=" << sum_cfg.cache.ttl_minutes
                                    << std::endl;
                        }
                    }

                    if (po.contains("task_memo") && po["task_memo"].is_object())
                    {
                        const auto &tm = po["task_memo"];
                        auto &memo_cfg = prompt_optimization_config_.task_memo;

                        memo_cfg.enabled = tm.value("enabled", false);
                        memo_cfg.model_layer_enabled = tm.value("model_layer_enabled", true);
                        memo_cfg.token_budget_ratio = tm.value("token_budget_ratio", 0.15);
                        memo_cfg.min_dropped_for_trigger = tm.value("min_dropped_for_trigger", (size_t) 1);
                        memo_cfg.render_throttle_min_new_dropped = tm.value("render_throttle_min_new_dropped", (size_t) 4);
                        memo_cfg.long_tool_chain_threshold = tm.value("long_tool_chain_threshold", (size_t) 4);
                        memo_cfg.low_confidence_threshold = tm.value("low_confidence_threshold", 0.5);
                        memo_cfg.rule_layer_preview_chars = tm.value("rule_layer_preview_chars", 160);
                        memo_cfg.memo_reserve_ratio = tm.value("memo_reserve_ratio", 0.3);
                        memo_cfg.goal_scan_window = tm.value("goal_scan_window", (size_t) 3);
                        memo_cfg.min_goal_signal_chars = tm.value("min_goal_signal_chars", (size_t) 20);
                        memo_cfg.goal_anchor_preview_chars = tm.value("goal_anchor_preview_chars", (size_t) 300);
                        memo_cfg.failure_streak_warn_threshold = tm.value("failure_streak_warn_threshold", 2);

                        if (tm.contains("page_directory") && tm["page_directory"].is_object())
                        {
                            const auto &pd = tm["page_directory"];
                            memo_cfg.page_directory.max_entries = pd.value("max_entries", (size_t) 20);
                        }

                        if (tm.contains("store") && tm["store"].is_object())
                        {
                            const auto &st = tm["store"];
                            memo_cfg.store.max_entries = st.value("max_entries", (size_t) 200);
                            memo_cfg.store.max_memory_mb = st.value("max_memory_mb", (size_t) 20);
                        }

                        My_Log{My_Log::Level::kInfo}
                                << "[TaskMemo] Config loaded: enabled=" << memo_cfg.enabled
                                << ", model_layer_enabled=" << memo_cfg.model_layer_enabled
                                << ", token_budget_ratio=" << memo_cfg.token_budget_ratio
                                << ", render_throttle_min_new_dropped=" << memo_cfg.render_throttle_min_new_dropped
                                << ", long_tool_chain_threshold=" << memo_cfg.long_tool_chain_threshold
                                << ", low_confidence_threshold=" << memo_cfg.low_confidence_threshold
                                << ", rule_layer_preview_chars=" << memo_cfg.rule_layer_preview_chars
                                << ", memo_reserve_ratio=" << memo_cfg.memo_reserve_ratio
                                << ", goal_scan_window=" << memo_cfg.goal_scan_window
                                << ", min_goal_signal_chars=" << memo_cfg.min_goal_signal_chars
                                << ", goal_anchor_preview_chars=" << memo_cfg.goal_anchor_preview_chars
                                << ", failure_streak_warn_threshold=" << memo_cfg.failure_streak_warn_threshold
                                << ", page_directory.max_entries=" << memo_cfg.page_directory.max_entries
                                << ", store.max_entries=" << memo_cfg.store.max_entries
                                << ", store.max_memory_mb=" << memo_cfg.store.max_memory_mb
                                << std::endl;

                        if (memo_cfg.enabled)
                        {
                            TaskMemoStore::GetInstance().Configure(memo_cfg.store);
                        }
                    }

                    My_Log{} << "Prompt engineering config loaded: "
                             << "output_reserve_ratio=" << prompt_optimization_config_.output_reserve_ratio
                             << ", recent_window=" << prompt_optimization_config_.recent_window
                             << ", old_compress_len=" << prompt_optimization_config_.old_compress_len
                             << ", recent_compress_len=" << prompt_optimization_config_.recent_compress_len
                             << ", format=" << prompt_optimization_config_.skill_catalog_format
                             << ", auto_correction=" << prompt_optimization_config_.enable_skill_auto_correction
                             << std::endl;
                }
            }
            catch (const std::exception &e)
            {
                My_Log{My_Log::Level::kError} << "Failed to load prompt_engineering.json: " << e.what() << std::endl;

            }
        }
    }

    {

        fs::path service_config_path = fs::path(RootDir) / "config" / "service_config.json";
        if (File::IsFileExist(service_config_path.generic_string()) &&
            !File::IsFileEmpty(service_config_path.generic_string()))
        {
            try
            {
                std::ifstream sc_file(service_config_path.generic_string());
                json sc_json;
                sc_file >> sc_json;

                bool is_stateless_mode = (getnumResponse() == -1);

                if (is_stateless_mode && sc_json.contains("routing"))
                {
                    ParseRoutingConfig(sc_json["routing"], routing_config_);
                }

                SharedCloudSettings shared;

                if (is_stateless_mode && sc_json.contains("cloud_shared"))
                {
                    ParseSharedCloudSettings(sc_json["cloud_shared"], shared);
                    My_Log{} << "[cloud_shared] Loaded: timeout=" << shared.timeout_seconds
                             << ", stream_timeout=" << shared.stream_timeout_seconds
                             << ", retry.max=" << shared.retry_max
                             << ", rate_limit.max_inferences=" << shared.rl_max_inferences
                             << std::endl;
                }

                if (is_stateless_mode && sc_json.contains("cloud_model"))
                {
                    const auto &cm = sc_json["cloud_model"];
                    cloud_model_config_.enabled = cm.value("enabled", false);
                    cloud_model_config_.base_url = cm.value("base_url", std::string(""));

                    cloud_model_config_.api_key = ResolveApiKey(
                            cm.value("api_key", std::string("")), "global::cloud_model");
                    cloud_model_config_.model = cm.value("model", std::string(""));
                    ApplyCloudSharedFields(cm, cloud_model_config_, shared);

                    if (cm.contains("upload_policy"))
                    {
                        const auto &up = cm["upload_policy"];
                        cloud_model_config_.upload_policy.enable_sensitivity_check =
                                up.value("enable_sensitivity_check", true);
                        cloud_model_config_.upload_policy.enable_desensitization =
                                up.value("enable_desensitization", true);
                    }

                    cloud_model_config_.context_size = cm.value("context_size", 0);
                    if (cloud_model_config_.context_size == 0)
                    {
                        My_Log{My_Log::Level::kWarning}
                                << "[Config] cloud_model.context_size not set, using default: "
                                << CloudModelConfig::DEFAULT_CLOUD_CONTEXT_SIZE
                                << ". Recommend setting context_size in service_config.json to match your cloud model."
                                << std::endl;
                    }

                    My_Log{} << "Cloud model config loaded: enabled=" << cloud_model_config_.enabled
                             << ", base_url=" << cloud_model_config_.base_url
                             << ", model=" << cloud_model_config_.model
                             << ", timeout=" << cloud_model_config_.timeout_seconds
                             << ", rate_limit.max_inferences_per_task="
                             << cloud_model_config_.rate_limit.max_inferences_per_task
                             << ", rate_limit.max_tokens_per_task="
                             << cloud_model_config_.rate_limit.max_tokens_per_task
                             << ", upload_policy.enable_sensitivity_check="
                             << cloud_model_config_.upload_policy.enable_sensitivity_check
                             << ", upload_policy.enable_desensitization="
                             << cloud_model_config_.upload_policy.enable_desensitization
                             << ", context_size=" << (cloud_model_config_.context_size > 0
                                                      ? cloud_model_config_.context_size
                                                      : CloudModelConfig::DEFAULT_CLOUD_CONTEXT_SIZE)
                             << (cloud_model_config_.context_size == 0 ? " (default)" : "")
                             << std::endl;
                }

                if (is_stateless_mode && sc_json.contains("enterprise_cloud_model"))
                {
                    const auto &ecm = sc_json["enterprise_cloud_model"];
                    enterprise_cloud_model_config_.enabled = ecm.value("enabled", false);
                    enterprise_cloud_model_config_.base_url = ecm.value("base_url", std::string(""));

                    enterprise_cloud_model_config_.api_key = ResolveApiKey(
                            ecm.value("api_key", std::string("")), "global::enterprise_cloud_model");
                    enterprise_cloud_model_config_.model = ecm.value("model", std::string(""));
                    ApplyCloudSharedFields(ecm, enterprise_cloud_model_config_, shared);

                    enterprise_cloud_model_config_.context_size = ecm.value("context_size", 0);
                    if (enterprise_cloud_model_config_.context_size == 0)
                    {
                        My_Log{My_Log::Level::kWarning}
                                << "[Config] enterprise_cloud_model.context_size not set, using default: "
                                << EnterpriseCloudModelConfig::DEFAULT_ENTERPRISE_CLOUD_CONTEXT_SIZE
                                << ". Recommend setting context_size in service_config.json to match your enterprise cloud model."
                                << std::endl;
                    }

                    My_Log{} << "Enterprise cloud model config loaded: enabled="
                             << enterprise_cloud_model_config_.enabled
                             << ", base_url=" << enterprise_cloud_model_config_.base_url
                             << ", model=" << enterprise_cloud_model_config_.model
                             << ", timeout=" << enterprise_cloud_model_config_.timeout_seconds
                             << ", rate_limit.max_inferences_per_task="
                             << enterprise_cloud_model_config_.rate_limit.max_inferences_per_task
                             << ", context_size=" << (enterprise_cloud_model_config_.context_size > 0
                                                      ? enterprise_cloud_model_config_.context_size
                                                      : EnterpriseCloudModelConfig::DEFAULT_ENTERPRISE_CLOUD_CONTEXT_SIZE)
                             << (enterprise_cloud_model_config_.context_size == 0 ? " (default)" : "")
                             << std::endl;
                }

                if (is_stateless_mode && sc_json.contains("local_model"))
                {
                    const auto &lm = sc_json["local_model"];
                    local_model_config_.enabled = lm.value("enabled", true);

                    My_Log{} << "Local model config loaded: enabled=" << local_model_config_.enabled
                             << std::endl;
                }

                if (sc_json.contains("prompt_optimization"))
                {
                    const auto &po = sc_json["prompt_optimization"];

                    if (po.contains("system_context") && po["system_context"].is_object())
                    {
                        const auto &sc = po["system_context"];
                        auto &ctx_cfg = prompt_optimization_config_.system_context;

                        if (sc.contains("sections") && sc["sections"].is_array())
                        {
                            ctx_cfg.sections.clear();
                            for (const auto &sec_json: sc["sections"])
                            {
                                if (!sec_json.is_object())
                                    continue;
                                SystemContextSection sec;
                                sec.title = sec_json.value("title", std::string(""));
                                sec.enabled = sec_json.value("enabled", true);

                                if (sec_json.contains("lines") && sec_json["lines"].is_array())
                                {
                                    for (const auto &line_json: sec_json["lines"])
                                    {
                                        if (line_json.is_string())
                                        {
                                            sec.lines.push_back(line_json.get<std::string>());
                                        }
                                    }
                                }
                                ctx_cfg.sections.push_back(sec);
                            }
                        }

                        My_Log{} << "[Config] system_context loaded: "
                                 << "sections=" << ctx_cfg.sections.size()
                                 << std::endl;
                    }

                    ParsePromptSectionsConfig(po, prompt_optimization_config_.prompt_sections, "prompt_sections");

                    ParsePromptSectionsConfig(po, prompt_optimization_config_.subagent_prompt_sections, "subagent_prompt_sections");

                    if (po.contains("system_prompts"))
                    {
                        const auto &sp = po["system_prompts"];
                        prompt_optimization_config_.system_prompts.identity_intro = sp.value("identity_intro", "");
                        prompt_optimization_config_.system_prompts.skill_rule = sp.value("skill_rule", "");
                        prompt_optimization_config_.system_prompts.tools_intro = sp.value("tools_intro", "");
                        prompt_optimization_config_.system_prompts.catalog_structured_intro = sp.value("catalog_structured_intro", "");

                        if (sp.contains("sections_enabled") && sp["sections_enabled"].is_object())
                        {
                            const auto &se = sp["sections_enabled"];
                            prompt_optimization_config_.system_prompts.sections_enabled.identity_intro = se.value("identity_intro", true);
                            prompt_optimization_config_.system_prompts.sections_enabled.skill_rule = se.value("skill_rule", true);
                            prompt_optimization_config_.system_prompts.sections_enabled.tools_intro = se.value("tools_intro", true);
                            prompt_optimization_config_.system_prompts.sections_enabled.catalog_structured_intro = se.value("catalog_structured_intro", true);
                            prompt_optimization_config_.system_prompts.sections_enabled.verification_first_rule = se.value("verification_first_rule", true);
                            My_Log{} << "[Config] system_prompts.sections_enabled loaded: "
                                     << "identity_intro="
                                     << prompt_optimization_config_.system_prompts.sections_enabled.identity_intro
                                     << ", skill_rule="
                                     << prompt_optimization_config_.system_prompts.sections_enabled.skill_rule
                                     << ", tools_intro="
                                     << prompt_optimization_config_.system_prompts.sections_enabled.tools_intro
                                     << ", catalog_structured_intro="
                                     << prompt_optimization_config_.system_prompts.sections_enabled.catalog_structured_intro
                                     << std::endl;
                        }

                        if (sp.contains("few_shot_examples_enabled") && sp["few_shot_examples_enabled"].is_object())
                        {
                            const auto &fe = sp["few_shot_examples_enabled"];
                            prompt_optimization_config_.system_prompts.few_shot_examples_enabled.enabled = fe.value("enabled", true);
                            prompt_optimization_config_.system_prompts.few_shot_examples_enabled.skill_correct_call = fe.value("skill_correct_call", true);
                            prompt_optimization_config_.system_prompts.few_shot_examples_enabled.no_skill_needed = fe.value("no_skill_needed", true);

                            prompt_optimization_config_.system_prompts.few_shot_examples_enabled.max_skill_examples = fe.value("max_skill_examples", 2);
                            My_Log{} << "[Config] system_prompts.few_shot_examples_enabled loaded: "
                                     << "enabled="
                                     << prompt_optimization_config_.system_prompts.few_shot_examples_enabled.enabled
                                     << ", skill_correct_call="
                                     << prompt_optimization_config_.system_prompts.few_shot_examples_enabled.skill_correct_call
                                     << ", no_skill_needed="
                                     << prompt_optimization_config_.system_prompts.few_shot_examples_enabled.no_skill_needed
                                     << ", max_skill_examples="
                                     << prompt_optimization_config_.system_prompts.few_shot_examples_enabled.max_skill_examples
                                     << std::endl;
                        }

                        if (sp.contains("few_shot_header"))
                            prompt_optimization_config_.system_prompts.few_shot_header = sp.value("few_shot_header", "## Examples\n\n");

                        if (sp.contains("few_shot_skill_title_template"))
                            prompt_optimization_config_.system_prompts.few_shot_skill_title_template = sp.value("few_shot_skill_title_template", "**Example {idx} - Skill Match**\n");
                        if (sp.contains("few_shot_default_user_query_prefix"))
                            prompt_optimization_config_.system_prompts.few_shot_default_user_query_prefix = sp.value("few_shot_default_user_query_prefix", "Please use the ");
                        if (sp.contains("few_shot_default_user_query_suffix"))
                            prompt_optimization_config_.system_prompts.few_shot_default_user_query_suffix = sp.value("few_shot_default_user_query_suffix", " skill");
                        if (sp.contains("few_shot_user_label"))
                            prompt_optimization_config_.system_prompts.few_shot_user_label = sp.value("few_shot_user_label", "User: ");
                        if (sp.contains("few_shot_response_label"))
                            prompt_optimization_config_.system_prompts.few_shot_response_label = sp.value("few_shot_response_label", "Response: ");
                        if (sp.contains("few_shot_correct_call_label"))
                            prompt_optimization_config_.system_prompts.few_shot_correct_call_label = sp.value("few_shot_correct_call_label", "Tool(correct skill call): ");
                        if (sp.contains("few_shot_no_skill_title_template"))
                            prompt_optimization_config_.system_prompts.few_shot_no_skill_title_template = sp.value("few_shot_no_skill_title_template", "**Example {idx} - List Skills (answer from catalog)**\n");
                        if (sp.contains("few_shot_no_skill_user_input"))
                            prompt_optimization_config_.system_prompts.few_shot_no_skill_user_input = sp.value("few_shot_no_skill_user_input", "What skills do you have? / 有哪些skills?");
                        if (sp.contains("few_shot_no_skill_response"))
                            prompt_optimization_config_.system_prompts.few_shot_no_skill_response = sp.value("few_shot_no_skill_response", "I have the following skills: [list from catalog above]. No tool call needed.");
                    }

                    if (po.contains("spawn_guard"))
                    {
                        const auto &sg = po["spawn_guard"];
                        prompt_optimization_config_.spawn_guard.enabled =
                                sg.value("enabled", true);
                        prompt_optimization_config_.spawn_guard.header =
                                sg.value("header", prompt_optimization_config_.spawn_guard.header);
                        prompt_optimization_config_.spawn_guard.body =
                                sg.value("body", prompt_optimization_config_.spawn_guard.body);
                        My_Log{My_Log::Level::kInfo}
                                << "[SpawnGuard] Config loaded: enabled="
                                << prompt_optimization_config_.spawn_guard.enabled
                                << std::endl;
                    }

                    My_Log{} << "Prompt optimization (client content) config loaded." << std::endl;
                }

            }
            catch (const std::exception &e)
            {
                My_Log{My_Log::Level::kError} << "Failed to load service_config.json: " << e.what() << std::endl;

            }
        }
    }

    return true;
}

bool ModelManager::LoadSingleModel()
{
    return LoadModel({model_name_, model_path_, config_file_});
}

bool ModelManager::LoadModel(const ModelLoadRequest &request)
{
    std::lock_guard<std::recursive_mutex> load_lock(load_mutex_);
    UnloadModelsByDevice(ResolveTentativeFamilies(request.path, request.config_file));

    json prompt = json::object();
    int context_size = DEFAULT_CONTEXT_SIZE;
    std::string known_model_path;
    PromptType prompt_type = LoadPromptTemplates(request.path + "/prompt.json", request.name,
                                                  request.path, prompt, context_size, known_model_path);
    {
        json prompt_copy = prompt;
        std::lock_guard<std::mutex> lock(models_mutex_);
        prompt_.swap(prompt_copy);
    }
    if (prompt_type == PromptType::Unknown)
        return false;
    My_Log{} << "check the prompt type: " << prompt_type.to_string() << "\n";

    const bool thinking_model = str_contains(request.name, "Qwen3") ||
                                str_contains(request.name, "DeepSeek") ||
                                str_contains(request.name, "Hunyuan");
    My_Log{} << "check if is thinking model: " << thinking_model << "\n";

    auto config = std::make_shared<ModelInstanceConfig>();
    config->set_model_name(request.name);
    config->set_model_path(request.path);
    config->set_config_file(request.config_file);
    config->set_known_model_path(known_model_path);
    config->set_context_size(context_size);
    config->set_prompt_template(prompt);
    config->set_prompt_type(prompt_type);
    config->set_thinking_model(thinking_model);
    std::string config_json_backend, config_json_device;
    ParseBackendDeviceFromConfigJson(request.config_file, request.name, "[LoadSingleModel]",
                                      config_json_backend, config_json_device);
    config->set_backend(!config_json_backend.empty() ? config_json_backend : "auto");
    if (!config_json_device.empty())
    {
        config->set_device(config_json_device);
    }
    config->set_lora_adapter(loraAdapter);
    config->set_lora_alpha(loraAlpha);
    config->set_output_all_text(outputAllText);
    config->set_enable_thinking(enableThinking);
    config->set_enable_prompt_debug(enablePromptDebug);
    config->set_num_response(num_response_);
    config->set_min_output_num(minOutputNum);

    auto model_handle = ModeVerifier::TryCreate(config.get(), this);
    if (!model_handle)
    {
        My_Log{My_Log::Level::kError} << RED
                                      << "Load Model Failed"
                                      << ", Model Name: " << request.name
                                      << ", Model Path: " << request.path
                                      << RESET << std::endl;
        return false;
    }

    {
        ModelFormat fmt = config->get_model_format();
        if (fmt == ModelFormat::QNN)
        {
            config->set_backend("qnn");
            config->set_device("npu");
        }
        else if (fmt == ModelFormat::MNN)
        {
            config->set_backend("mnn");
            config->set_device("cpu");
        }
        else if (fmt == ModelFormat::GGUF)
        {
            config->set_backend("GGUF");
            std::string gguf_device = config->get_device();
            std::transform(gguf_device.begin(), gguf_device.end(), gguf_device.begin(),
                           [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
            if (gguf_device == "cpu" || gguf_device == "gpu")
            {
                config->set_device(gguf_device);
            }
            else
            {
                config->set_device("gpu");
            }
        }
        My_Log{} << "LoadSingleModel: model_format=" << const_cast<ModelFormat &>(fmt).to_string()
                 << ", backend=" << config->get_backend()
                 << ", device=" << config->get_device() << std::endl;
    }

    std::vector<std::shared_ptr<LoadedModel>> replaced_models;
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        auto loaded_model = std::make_shared<LoadedModel>();
        loaded_model->config = config;
        loaded_model->context = model_handle;
        loaded_model->backend = config->get_backend();
        loaded_model->device = config->get_device();
        loaded_model->is_loaded = true;
        loaded_model->occupied_families = ComputeOccupiedFamilies(*loaded_model);

        auto existing = loaded_models_.find(request.name);
        if (existing != loaded_models_.end())
        {
            My_Log{My_Log::Level::kWarning} << "[LoadModel] replacing already registered model '"
                                            << request.name << "'" << std::endl;
            replaced_models.push_back(std::move(existing->second));
        }
        loaded_models_[request.name] = loaded_model;
        default_model_name_ = request.name;
        genieModelHandle = model_handle;
    }
    if (!replaced_models.empty())
    {
        StopAndDrain(replaced_models, std::chrono::seconds(30));
    }

    My_Log{} << GREEN << "Model load successfully: " << request.name << RESET << std::endl;
    return true;

}

void ModelManager::UnloadModel()
{
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        for (auto &entry: loaded_models_)
        {
            My_Log{} << "UnloadModel: releasing model '"
                     << (entry.second && entry.second->config ? entry.second->config->get_model_name() : entry.first)
                     << "'" << std::endl;
        }
    }

    Clean();
}

bool ModelManager::UnloadModelByName(const std::string &model_name)
{
    std::lock_guard<std::recursive_mutex> load_lock(load_mutex_);
    std::vector<std::shared_ptr<LoadedModel>> removed;
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        std::string name_lower = model_name;
        std::transform(name_lower.begin(), name_lower.end(), name_lower.begin(),
                       [](unsigned char c) { return static_cast<unsigned char>(std::tolower(c)); });
        for (auto it = loaded_models_.begin(); it != loaded_models_.end(); ++it)
        {
            if (!it->second || !it->second->config)
                continue;
            std::string entry_lower = it->second->config->get_model_name();
            std::transform(entry_lower.begin(), entry_lower.end(), entry_lower.begin(),
                           [](unsigned char c) { return static_cast<unsigned char>(std::tolower(c)); });
            if (entry_lower != name_lower)
                continue;
            My_Log{} << "UnloadModel: releasing model '" << it->first << "'" << std::endl;
            const bool was_mirror = it->second->context == genieModelHandle;
            removed.push_back(it->second);
            loaded_models_.erase(it);
            if (was_mirror)
            {
                auto fallback = GetDefaultModelLocked();
                genieModelHandle = fallback ? fallback->context : nullptr;
            }
            break;
        }
    }
    if (removed.empty())
        return false;

    StopAndDrain(removed, std::chrono::seconds(30));

    const bool had_qnn = removed.front() && removed.front()->backend == "qnn";
    if (had_qnn && removed.front()->config)
    {
        removed.front()->config->i_model_config_.qnn_embedding_.Clean();
    }
    removed.clear();
    if (had_qnn)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(2000));
    }
    return true;
}

std::vector<json> ModelManager::ScanModelDirectory() const
{
    if (model_root_.empty())
    {
        My_Log{My_Log::Level::kWarning} << "[ScanModelDirectory] model_root_ is empty, cannot scan." << std::endl;
        return {};
    }

    std::vector<json> result;
    std::error_code ec;
    fs::directory_iterator dir_it(model_root_, ec);
    if (ec)
    {
        My_Log{My_Log::Level::kWarning}
                << "[ScanModelDirectory] Failed to open directory '" << model_root_
                << "': " << ec.message() << std::endl;
        return {};
    }
    for (const auto &entry: dir_it)
    {
        if (!entry.is_directory())
            continue;

        std::string config_path = File::ResolveModelConfigPath(entry.path().generic_string());
        if (!File::IsFileExist(config_path))
            continue;

        std::string name = entry.path().filename().generic_string();
        json m;
        m["id"] = name;

        int ctx = 0;
        std::string backend = "unknown";
        std::string device = "unknown";

        if (File::IsFileEmpty(config_path))
        {
            backend = "GGUF";
            device = "gpu";
        }
        else
        {
            bool has_gguf_file = false;
            for (const auto &sub_entry: fs::directory_iterator(entry.path(), ec))
            {
                if (!ec && sub_entry.is_regular_file() &&
                    sub_entry.path().extension() == ".gguf")
                {
                    has_gguf_file = true;
                    break;
                }
            }

            if (has_gguf_file)
            {
                backend = "GGUF";
                device = "gpu";
            }
            else
            {
                try
                {
                    std::ifstream f(config_path);
                    json cfg;
                    f >> cfg;

                    if (cfg.contains("dialog") && cfg["dialog"].contains("context"))
                    {
                        ctx = cfg["dialog"]["context"].value("size", 0);
                        backend = "qnn";
                        device = "npu";
                    }
                    else if (cfg.contains("llm_model"))
                    {
                        ctx = cfg.value("context_size", cfg.value("context_length", 0));
                        backend = "mnn";
                        device = "cpu";
                    }
                    else
                    {
                        backend = "GGUF";
                        device = "gpu";
                    }
                }
                catch (const std::exception &e)
                {
                    My_Log{My_Log::Level::kWarning}
                            << "[ScanModelDirectory] Failed to parse config.json for model '" << name
                            << "': " << e.what() << std::endl;
                    backend = "GGUF";
                    device = "gpu";
                }
            }
        }

        if (ctx == 0)
        {
            std::string prompt_path = entry.path().generic_string() + "/prompt.json";
            if (File::IsFileExist(prompt_path))
            {
                try
                {
                    std::ifstream f(prompt_path);
                    json p;
                    f >> p;
                    ctx = p.value("context_size", 0);
                }
                catch (...)
                {
                }
            }
        }

        m["context_length"] = ctx;
        m["backend"] = backend;
        m["device"] = device;
        result.push_back(m);
    }

    My_Log{} << "[ScanModelDirectory] Found " << result.size()
             << " model(s) in " << model_root_ << std::endl;
    return result;
}

std::vector<std::string> ModelManager::ComputeOccupiedFamilies(const LoadedModel &model)
{
    if (model.backend == "qnn")
    {
        return {"npu"};
    }
    if (model.backend == "mnn")
    {
        return {"cpu"};
    }
    if (model.backend == "GGUF")
    {
        if (model.device == "cpu")
        {
            return {"cpu"};
        }
        if (model.config && model.config->is_speculative_draft_forced_cpu())
        {
            return {"gpu", "cpu"};
        }
        return {"gpu"};
    }
    if (!model.device.empty())
    {
        return {model.device};
    }
    return {};
}

std::vector<std::string> ModelManager::ResolveTentativeFamilies(const std::string &model_path,
                                                                 const std::string &config_file)
{
    bool has_gguf_file = false;
    std::error_code ec;
    for (const auto &entry: fs::directory_iterator(model_path, ec))
    {
        if (!ec && entry.is_regular_file() && entry.path().extension() == ".gguf")
        {
            has_gguf_file = true;
            break;
        }
    }

    json cfg;
    bool cfg_parsed = false;
    if (File::IsFileExist(config_file) && !File::IsFileEmpty(config_file))
    {
        try
        {
            std::ifstream f(config_file);
            f >> cfg;
            cfg_parsed = true;
        }
        catch (...)
        {
        }
    }

    if (!has_gguf_file && cfg_parsed)
    {
        if (cfg.contains("dialog") && cfg["dialog"].contains("context"))
        {
            return {"npu"};
        }
        if (cfg.contains("llm_model"))
        {
            return {"cpu"};
        }
    }

    std::string backend_field, device_field;
    ParseBackendDeviceFromConfigJson(config_file, "", "[ResolveTentativeFamilies]", backend_field, device_field);
    std::transform(backend_field.begin(), backend_field.end(), backend_field.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    std::transform(device_field.begin(), device_field.end(), device_field.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });

    if (device_field == "cpu" || backend_field == "cpu")
    {
        return {"cpu"};
    }

    if (cfg_parsed)
    {
        llama_speculative::DraftModelConfig draft_config = llama_speculative::DraftModelConfig::ParseFrom(cfg);
        if (draft_config.present &&
            llama_speculative::GetTotalPhysicalMemoryBytes() <= (32ULL * 1024 * 1024 * 1024))
        {
            return {"gpu", "cpu"};
        }
    }

    return {"gpu"};
}

void ModelManager::StopAndDrain(std::vector<std::shared_ptr<LoadedModel>> &models, std::chrono::seconds timeout,
                                const std::shared_ptr<ContextBase> &extra_context)
{
    std::vector<std::shared_ptr<ContextBase>> contexts_to_stop;
    if (extra_context)
    {
        contexts_to_stop.push_back(extra_context);
    }
    for (auto &model: models)
    {
        if (model && model->context &&
            std::find(contexts_to_stop.begin(), contexts_to_stop.end(), model->context) == contexts_to_stop.end())
        {
            contexts_to_stop.push_back(model->context);
        }
    }
    std::vector<std::thread> stop_threads;
    stop_threads.reserve(contexts_to_stop.size());
    for (auto &context: contexts_to_stop)
    {
        stop_threads.emplace_back([context]() { context->Stop(); });
    }
    for (auto &stop_thread: stop_threads)
    {
        stop_thread.join();
    }
    contexts_to_stop.clear();

    const auto drain_deadline = std::chrono::steady_clock::now() + timeout;
    auto in_flight = [&models]()
    {
        for (const auto &model: models)
        {
            if (model && model.use_count() > 1)
                return true;
        }
        return false;
    };
    while (in_flight() && std::chrono::steady_clock::now() < drain_deadline)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    if (in_flight())
    {
        My_Log{My_Log::Level::kWarning}
                << "[UnloadModelsByDevice] evicted model still referenced by in-flight requests after "
                << timeout.count() << "s, releasing anyway"
                << std::endl;
    }
}

void ModelManager::UnloadModelsByDevice(const std::vector<std::string> &families)
{
    std::vector<std::shared_ptr<LoadedModel>> evicted_models;
    bool default_handle_evicted = false;
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        for (auto it = loaded_models_.begin(); it != loaded_models_.end();)
        {
            bool conflicts = std::any_of(
                    it->second->occupied_families.begin(), it->second->occupied_families.end(),
                    [&families](const std::string &family)
                    {
                        return std::find(families.begin(), families.end(), family) != families.end();
                    });
            if (conflicts)
            {
                My_Log{} << "[UnloadModelsByDevice] evicting '" << it->first << "'" << std::endl;
                default_handle_evicted = default_handle_evicted ||
                                         (it->second && it->second->context == genieModelHandle);
                evicted_models.push_back(it->second);
                it = loaded_models_.erase(it);
            }
            else
            {
                ++it;
            }
        }
        if (default_handle_evicted)
        {
            auto default_model = GetDefaultModelLocked();
            genieModelHandle = default_model ? default_model->context : nullptr;
        }
    }

    StopAndDrain(evicted_models, std::chrono::seconds(30));

    bool had_qnn_eviction = false;
    for (auto &evicted: evicted_models)
    {
        if (!evicted)
            continue;
        if (evicted->backend == "qnn")
        {
            had_qnn_eviction = true;
            if (evicted->config)
            {
                evicted->config->i_model_config_.qnn_embedding_.Clean();
            }
        }
    }
    evicted_models.clear();
    if (had_qnn_eviction)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(2000));
    }
}

std::shared_ptr<LoadedModel> ModelManager::GetModel(const std::string &model_name)
{
    std::lock_guard<std::mutex> lock(models_mutex_);
    if (model_name.empty())
    {
        return nullptr;
    }

    std::string name_lower = model_name;
    std::transform(name_lower.begin(), name_lower.end(), name_lower.begin(),
                   [](unsigned char c) { return static_cast<unsigned char>(std::tolower(c)); });
    for (const auto &entry: loaded_models_)
    {
        if (!entry.second || !entry.second->config)
            continue;
        std::string entry_name_lower = entry.second->config->get_model_name();
        std::transform(entry_name_lower.begin(), entry_name_lower.end(), entry_name_lower.begin(),
                       [](unsigned char c) { return static_cast<unsigned char>(std::tolower(c)); });
        if (entry_name_lower == name_lower)
        {
            return entry.second;
        }
    }
    return nullptr;
}

std::shared_ptr<LoadedModel> ModelManager::GetDefaultModel()
{
    std::lock_guard<std::mutex> lock(models_mutex_);
    return GetDefaultModelLocked();
}

std::shared_ptr<LoadedModel> ModelManager::GetDefaultModelLocked() const
{
    auto it = loaded_models_.find(default_model_name_);
    if (it != loaded_models_.end())
    {
        return it->second;
    }
    if (!loaded_models_.empty())
    {
        return loaded_models_.begin()->second;
    }
    return nullptr;
}

std::vector<std::string> ModelManager::ListLoadedModels() const
{
    std::lock_guard<std::mutex> lock(models_mutex_);
    std::vector<std::string> names;
    names.reserve(loaded_models_.size());
    for (const auto &entry: loaded_models_)
    {
        if (entry.second && entry.second->config)
        {
            names.push_back(entry.second->config->get_model_name());
        }
    }
    return names;
}

std::vector<std::shared_ptr<LoadedModel>> ModelManager::GetAllLoadedModels() const
{
    std::lock_guard<std::mutex> lock(models_mutex_);
    std::vector<std::shared_ptr<LoadedModel>> models;
    models.reserve(loaded_models_.size());
    for (const auto &entry: loaded_models_)
    {
        if (entry.second)
        {
            models.push_back(entry.second);
        }
    }
    return models;
}

uint64_t ModelManager::EstimateOtherLoadedModelsMemoryBytes(const std::string &exclude_name) const
{
    std::lock_guard<std::mutex> lock(models_mutex_);
    uint64_t total = 0;
#ifdef USE_MNN
    for (const auto &entry: loaded_models_)
    {
        if (!entry.second || !entry.second->config)
            continue;
        if (entry.second->config->get_model_name() == exclude_name)
            continue;
        total += MNNContext::EstimateMnnMemoryRequirement(entry.second->config->get_model_path());
    }
#endif
    return total;
}

static int ParseContextSizeFromConfigJson(const std::string &config_json_path,
                                          const std::string &model_name,
                                          const std::string &log_prefix)
{
    int config_json_ctx_size = 0;
    if (File::IsFileExist(config_json_path) && !File::IsFileEmpty(config_json_path))
    {
        {
            std::error_code abs_ec;
            fs::path abs_path = fs::weakly_canonical(fs::path(config_json_path), abs_ec);
            My_Log{My_Log::Level::kInfo}
                    << log_prefix << " Reading model config file for context_size: "
                    << (abs_ec ? config_json_path : abs_path.generic_string()) << " (model='" << model_name << "')\n";
            const fs::path shadowed = fs::path(config_json_path).parent_path() / "config.json";
            if (fs::path(config_json_path).filename() == "genie_config.json" && File::IsFileExist(shadowed.string()))
            {
                My_Log{My_Log::Level::kWarning}
                        << log_prefix << " genie_config.json takes precedence; sibling config.json is IGNORED: "
                        << shadowed.generic_string() << "\n";
            }
        }
        try
        {
            std::ifstream f(config_json_path);
            json cfg;
            f >> cfg;
            if (cfg.contains("dialog") && cfg["dialog"].contains("context"))
            {
                config_json_ctx_size = cfg["dialog"]["context"].value("size", 0);
                if (config_json_ctx_size > 0)
                {
                    My_Log{My_Log::Level::kInfo}
                            << log_prefix << " Read context_size from config.json dialog.context.size="
                            << config_json_ctx_size << " for model '" << model_name << "'\n";
                }
            }
            if (config_json_ctx_size <= 0)
            {
                config_json_ctx_size = cfg.value("context_size", 0);
                if (config_json_ctx_size > 0)
                {
                    My_Log{My_Log::Level::kInfo}
                            << log_prefix << " Read context_size from config.json top-level context_size="
                            << config_json_ctx_size << " for model '" << model_name << "'\n";
                }
            }
        }
        catch (const std::exception &e)
        {
            My_Log{My_Log::Level::kWarning}
                    << log_prefix << " Failed to parse config.json for context_size, model='"
                    << model_name << "': " << e.what() << "\n";
        }
    }
    return config_json_ctx_size;
}

static void ParseBackendDeviceFromConfigJson(const std::string &config_json_path,
                                             const std::string &model_name,
                                             const std::string &log_prefix,
                                             std::string &out_backend,
                                             std::string &out_device)
{
    out_backend.clear();
    out_device.clear();
    if (!File::IsFileExist(config_json_path) || File::IsFileEmpty(config_json_path))
    {
        return;
    }
    try
    {
        std::ifstream f(config_json_path);
        json cfg;
        f >> cfg;
        out_backend = cfg.value("backend", std::string(""));
        out_device = cfg.value("device", std::string(""));
        if (!out_backend.empty() || !out_device.empty())
        {
            My_Log{My_Log::Level::kInfo}
                    << log_prefix << " Read backend/device from config.json: backend='" << out_backend
                    << "', device='" << out_device << "' for model '" << model_name << "'\n";
        }
    }
    catch (const std::exception &e)
    {
        My_Log{My_Log::Level::kWarning}
                << log_prefix << " Failed to parse config.json for backend/device, model='"
                << model_name << "': " << e.what() << "\n";
    }
}

static void ParsePromptFile(const std::string &prompt_path,
                            const std::string &model_name,
                            json &out_prompt,
                            PromptType &out_pt,
                            int &out_ctx_size)
{

    out_prompt = json::object();
    out_pt = PromptType::Unknown;
    out_ctx_size = 0;

    My_Log{My_Log::Level::kInfo}
            << "[ParsePromptFile] Checking prompt file for model '" << model_name
            << "', path='" << prompt_path << "'\n";
    My_Log{My_Log::Level::kInfo}
            << "[ParsePromptFile] File exists: " << File::IsFileExist(prompt_path)
            << ", File empty: " << File::IsFileEmpty(prompt_path) << "\n";

    if (!File::IsFileExist(prompt_path) || File::IsFileEmpty(prompt_path))
    {
        My_Log{My_Log::Level::kWarning}
                << "[ParsePromptFile] Prompt file not found or empty for model '"
                << model_name << "', path='" << prompt_path << "'.\n";
        return;
    }

    try
    {
        std::ifstream file(prompt_path);
        if (!file.good())
        {
            My_Log{My_Log::Level::kError}
                    << "[ParsePromptFile] Failed to open prompt.json for model '"
                    << model_name << "': file.good()=false, path='" << prompt_path << "'.\n";

            return;
        }

        json j;
        file >> j;
        My_Log{My_Log::Level::kInfo} << "[ParsePromptFile] Parsed prompt.json, type=" << j.type_name()
                                     << ", is_object=" << j.is_object()
                                     << " for model '" << model_name << "'\n";

        if (!j.is_object())
        {
            My_Log{My_Log::Level::kError}
                    << "[ParsePromptFile] prompt.json root is not a JSON object (type=" << j.type_name()
                    << ") for model '" << model_name << "'. "
                    << "Expected an object with keys: prompt_system, prompt_user, prompt_assistant, etc.\n";

            return;
        }

        out_prompt = json::object();
        out_prompt["system"] = j.value("prompt_system", std::string(""));
        out_prompt["user"] = j.value("prompt_user", std::string(""));
        out_prompt["assistant"] = j.value("prompt_assistant", std::string(""));
        out_prompt["tool"] = j.value("prompt_tool", std::string(""));
        out_prompt["start"] = j.value("prompt_start", std::string(""));
        if (j.contains("knowledge_cutoff"))
            out_prompt["knowledge_cutoff"] = j.value("knowledge_cutoff", std::string("2024-06"));
        if (j.contains("reasoning_level"))
            out_prompt["reasoning_level"] = j.value("reasoning_level", std::string("medium"));

        {
            auto sys_str = out_prompt["system"].get<std::string>();
            auto user_str = out_prompt["user"].get<std::string>();
            auto asst_str = out_prompt["assistant"].get<std::string>();
            auto tool_str = out_prompt["tool"].get<std::string>();
            auto start_str = out_prompt["start"].get<std::string>();
            My_Log{My_Log::Level::kInfo}
                    << "[ParsePromptFile] prompt fields loaded for model '" << model_name << "':"
                    << " system_len=" << sys_str.size()
                    << ", user_len=" << user_str.size()
                    << ", assistant_len=" << asst_str.size()
                    << ", tool_len=" << tool_str.size()
                    << ", start_len=" << start_str.size() << "\n";
            My_Log{My_Log::Level::kInfo}
                    << "[ParsePromptFile] system_preview='"
                    << (sys_str.size() > 40 ? sys_str.substr(0, 40) : sys_str) << "...', "
                    << "assistant_preview='"
                    << (asst_str.size() > 40 ? asst_str.substr(0, 40) : asst_str) << "...'\n";
        }

        if (j.contains("context_size"))
        {
            if (j["context_size"].is_string())
                out_ctx_size = std::stoi(j["context_size"].get<std::string>());
            else if (j["context_size"].is_number())
                out_ctx_size = j["context_size"].get<int>();
            else
                My_Log{My_Log::Level::kWarning}
                        << "[ParsePromptFile] context_size has invalid type in prompt.json for model '"
                        << model_name << "', ignored.\n";
        }

        out_pt = str_contains(out_prompt["assistant"].get<std::string>(), "<|channel|>")
                 ? PromptType{PromptType::Harmony}
                 : PromptType{PromptType::General};

        My_Log{My_Log::Level::kInfo} << "[ParsePromptFile] SUCCESS: prompt_type=" << out_pt.to_string()
                                     << ", context_size=" << out_ctx_size
                                     << ", out_prompt.type='" << out_prompt.type_name() << "'"
                                     << ", out_prompt.is_object=" << out_prompt.is_object()
                                     << " for model '" << model_name << "'\n";
    }
    catch (const std::exception &e)
    {
        My_Log{My_Log::Level::kError}
                << "[ParsePromptFile] EXCEPTION loading prompt file '" << prompt_path
                << "' for model '" << model_name << "': " << e.what() << "\n";

        out_prompt = json::object();
        out_pt = PromptType::Unknown;
        out_ctx_size = 0;
    }
}

PromptType ModelManager::LoadPromptTemplates(std::string &&prompt_path, const std::string &model_name,
                                             const std::string &model_path, json &out_prompt, int &out_ctx_size,
                                             std::string &out_known_model_path)
{
    PromptType pt{PromptType::Unknown};
    out_ctx_size = DEFAULT_CONTEXT_SIZE;
    out_known_model_path.clear();

    if (!File::IsFileExist(prompt_path) || File::IsFileEmpty(prompt_path))
    {
        std::string org_prompt_path = prompt_path;
        out_known_model_path = ResolveKnownModelPath(model_name, false);
        if (!out_known_model_path.empty())
        {
            prompt_path = out_known_model_path + "/prompt.json";
            My_Log{} << "get known model path successfully\n";
            goto ahead;
        }

        static std::array<std::string, 9> models_prefix{
                "allam-7b-ssd",
                "deepseek-r1-distill-qwen-7B",
                "hunyuan2B",
                "ibm-granite-v3.1-8b",
                "llama2.0-7b",
                "llama3.1-8b",
                "phi",
                "qwen",
                "gpt-oss-20b",
        };

        prompt_path.clear();
        for (const auto &model_prefix: models_prefix)
        {

            if (ModelComparer(model_name, model_prefix, true))
            {

                prompt_path = ResolveKnownModelPath(model_prefix, true);
                break;
            }
        }

        if (prompt_path.empty())
        {
            My_Log{My_Log::Level::kError} << "prompt file: " << org_prompt_path << " "
                                          << "is not exist, and not match any config models while finding"
                                          << std::endl;
            return pt;
        }

        prompt_path += "/prompt.json";
        ahead:
        My_Log{My_Log::Level::kError} << "prompt file: " << org_prompt_path << " "
                                      << "is not exist, will use default ver: " << prompt_path
                                      << std::endl;
    }

    int json_ctx_size = 0;
    ParsePromptFile(prompt_path, model_name, out_prompt, pt, json_ctx_size);

    if (pt == PromptType::Unknown)
    {
        return pt;
    }

    std::string base_path = !out_known_model_path.empty() ? out_known_model_path : model_path;
    int config_json_ctx_size = ParseContextSizeFromConfigJson(File::ResolveModelConfigPath(base_path), model_name, "[LoadPromptTemplates]");

    if (config_json_ctx_size > 0)
    {
        out_ctx_size = config_json_ctx_size;
        My_Log{My_Log::Level::kInfo} << "contextSize (from config.json): " << out_ctx_size << std::endl;
    }
    else if (json_ctx_size > 0)
    {
        out_ctx_size = json_ctx_size;
        My_Log{My_Log::Level::kInfo} << "contextSize (from prompt.json): " << out_ctx_size << std::endl;
    }
    else
    {
        out_ctx_size = DEFAULT_CONTEXT_SIZE;
        My_Log{My_Log::Level::kWarning}
                << "context_size field missing or invalid in both config.json and prompt.json, using default: "
                << out_ctx_size << std::endl;
    }

    return pt;
}

void IModelConfig::UpdateModeList() const
{
    std::vector<std::string> names;
    for (const auto &entry: fs::directory_iterator(model_root_))
    {
        if (!entry.is_directory())
        {
            continue;
        }
        names.push_back(entry.path().filename().generic_string());
    }
    std::lock_guard<std::mutex> lock(*model_list_mutex_);
    model_list_.swap(names);
}

void ModelManager::Clean()
{

    std::vector<std::shared_ptr<LoadedModel>> remaining;
    std::shared_ptr<ContextBase> default_handle;
    bool had_other_qnn = false;
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        for (auto &entry: loaded_models_)
        {
            if (entry.second && entry.second->backend == "qnn")
            {
                had_other_qnn = true;
            }
            remaining.push_back(entry.second);
        }
        loaded_models_.clear();
        default_model_name_.clear();
        default_handle = std::move(genieModelHandle);
        genieModelHandle = nullptr;
        known_model_path_.clear();
        model_name_.clear();
    }
    bool had_qnn_handle = (default_handle != nullptr);
    StopAndDrain(remaining, std::chrono::seconds(6), default_handle);
    bool had_embedding_handle = std::any_of(
            remaining.begin(), remaining.end(),
            [](const auto &model)
            {
                if (!model || !model->config)
                    return false;
                const auto &resources = model->config->i_model_config_.get_qnn_embedding().infer_resources_;
                return std::any_of(resources.begin(), resources.end(),
                                   [](const auto &entry) { return entry.second.app_builder_ != nullptr; });
            });
    default_handle.reset();
    remaining.clear();
    if (had_qnn_handle || had_other_qnn || had_embedding_handle)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(
                (had_qnn_handle || had_other_qnn) && had_embedding_handle ? 4000 : 2000));
    }
}

std::string ModelManager::ResolveKnownModelPath(const std::string &model_feature, bool only_prefix)
{
    std::call_once(config_list_once_,
                   [this]()
                   {
                       fs::path config_path{RootDir + "/config"};
                       if (!fs::is_directory(config_path))
                       {
                           return;
                       }
                       for (const auto &entry: fs::directory_iterator(RootDir + "/config"))
                       {
                           if (!entry.is_directory())
                           {
                               continue;
                           }
                           config_model_name_list_.push_back(entry.path().filename().generic_string());
                       }
                   });

    for (const auto &config_model_name: config_model_name_list_)
    {
        if (!ModelComparer(config_model_name, model_feature, only_prefix))
        {
            continue;
        }
        return RootDir + "/config/" + config_model_name;
    }

    return "";
}

bool ModelManager::ModelComparer(const std::string &source, const std::string &target, bool only_prefix)
{
    std::string s = source;
    std::string t = target;
    static auto normalize{
            [](std::string &s)
            {
                std::transform(s.begin(), s.end(), s.begin(),
                               [](unsigned char c) -> unsigned char
                               {
                                   if (c == '-' || c == '.' || c == ' ')
                                       return '_';
                                   return static_cast<unsigned char>(std::tolower(c));
                               });
            }
    };

    normalize(s);
    normalize(t);
    return only_prefix ? s.find(t) == 0 : s == t;
}

void QNNEmbedding::Clean()
{
    embedded_raw_buf_.clear();
    for (auto &infer_resource: infer_resources_)
    {
        auto &app_builder = infer_resource.second.app_builder_;
        if (app_builder != nullptr)
        {
            app_builder->ModelDestroy(infer_resource.second.tag_);
            delete app_builder;
            app_builder = nullptr;
        }
    }
    infer_resources_.clear();
    embedding_type_ = QNNEmbeddingType::None;
    model_types_ = ModelType::Unknown;
    data_type = EmbeddingDataType::None;
}

LibAppBuilder *QNNEmbedding::LibAppbuilderCreator(const std::string &serialized_file,
                                                  const std::string &tag)
{
#ifdef WIN32
#define BACKEND "QnnHtp.dll"
#define SYSTEM "QnnSystem.dll"
#else
#define BACKEND "libQnnHtp.so"
#define SYSTEM "libQnnSystem.so"
#endif
    static bool log_setting{
            []()
            {
                SetLogLevel(My_Log::Level_, "");
                return true;
            }()
    };

    auto *app_builder = new LibAppBuilder{};
    My_Log{} << "start to initiate: " << serialized_file << " ....\n";
    if (!app_builder->ModelInitialize(tag,
                                      serialized_file,
                                      BACKEND,
                                      SYSTEM))
    {
        My_Log("call model initialize failed");
        return nullptr;
    }
    return app_builder;
}
