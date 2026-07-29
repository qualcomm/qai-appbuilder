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
#include "../context/qnn/genie.h"
#include "../context/mnn.h"
#include "../context/llama_cpp.h"
#include "../response/response_tools.h"
#include "../chat_request_handler/summary_cache.h"
#include <algorithm>
#include <cctype>
#include <filesystem>
#include <thread>
#include <chrono>

#ifdef _WIN32

#include <wincred.h>

#endif

namespace fs = std::filesystem;

// ── Windows Credential Manager API Key 读取辅助函数 ──────────────────────────
// 服务名与 QAIAgentForge/backend/keyring_helper.py 中的 SERVICE_NAME 保持一致。
// account 格式：
//   "global::cloud_model"            → cloud_model.api_key
//   "global::enterprise_cloud_model" → enterprise_cloud_model.api_key
//
// keyring 库（WinVaultKeyring）的存储规则：
//   - 第一次存储：TargetName = SERVICE_NAME（"QAIAgentForge"），UserName = account
//   - 有冲突时：TargetName = "{account}@{SERVICE_NAME}"（"global::cloud_model@QAIAgentForge"）
//   - CredentialBlob 编码：UTF-16 LE（keyring 库写入时使用 UTF-16）
//
// 优先级：Credential Manager > JSON 文件（fallback）
// 若 Credential Manager 中无值或平台不支持，返回空字符串，由调用方 fallback 到 JSON 值。
static std::string LoadApiKeyFromCredentialManager(const std::string &account)
{
#ifdef _WIN32
    // Credential Manager 服务名（与 keyring_helper.py 中 SERVICE_NAME = "QAIAgentForge" 一致）
    const std::wstring service_name = L"QAIAgentForge";
    const std::wstring account_w(account.begin(), account.end());

    // keyring WinVaultKeyring 的读取逻辑（_resolve_credential）：
    //   1. 先尝试 TargetName = SERVICE_NAME，检查 UserName 是否匹配
    //   2. 若不匹配，再尝试 TargetName = "{account}@{SERVICE_NAME}"
    // 此处按相同顺序尝试两个 target name。

    auto try_read = [&](const std::wstring &target) -> std::string
    {
        PCREDENTIALW pcred = nullptr;
        if (!CredReadW(target.c_str(), CRED_TYPE_GENERIC, 0, &pcred) || pcred == nullptr)
            return "";

        std::string result;
        // 验证 UserName 是否与 account 匹配（keyring 存储时 UserName = account）
        bool user_match = (pcred->UserName != nullptr &&
                           std::wstring(pcred->UserName) == account_w);

        if (user_match && pcred->CredentialBlobSize > 0 && pcred->CredentialBlob != nullptr)
        {
            // keyring 库写入时使用 UTF-16 LE 编码
            // 将 UTF-16 LE 字节序列转换为 UTF-8 std::string
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

    // 尝试1：TargetName = "QAIAgentForge"（单账号场景，keyring 默认存储位置）
    std::string key = try_read(service_name);
    if (!key.empty())
    {
        My_Log{} << "[CredMgr] API Key loaded from Credential Manager (primary target): account=" << account
                 << std::endl;
        return key;
    }

    // 尝试2：TargetName = "global::cloud_model@QAIAgentForge"（多账号冲突场景）
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

// ── API Key 解析辅助函数 ──────────────────────────────────────────────────────
// 优先从 Windows Credential Manager 读取，若无值则 fallback 到 JSON 中的值。
// json_api_key: 从 service_config.json 中读取的原始值（可能为空字符串）
// account:      Credential Manager 账号名（如 "global::cloud_model"）
static std::string ResolveApiKey(const std::string &json_api_key, const std::string &account)
{
    std::string cm_key = LoadApiKeyFromCredentialManager(account);
    if (!cm_key.empty())
    {
        return cm_key;
    }
    // fallback：使用 JSON 中的值（用户直接编辑配置文件的场景）
    if (!json_api_key.empty())
    {
        My_Log{} << "[CredMgr] API Key fallback to JSON value: account=" << account << std::endl;
    }
    return json_api_key;
}

#include <LibAppBuilder.hpp>

// 前向声明：ParsePromptFile 定义在文件后部，LoadModel 和 LoadPromptTemplates 均调用此函数
static void ParsePromptFile(const std::string &prompt_path,
                            const std::string &model_name,
                            json &out_prompt,
                            PromptType &out_pt,
                            int &out_ctx_size);

// 前向声明：ParseContextSizeFromConfigJson 定义在文件后部，LoadModel 和 LoadPromptTemplates 均调用此函数
static int ParseContextSizeFromConfigJson(const std::string &config_json_path,
                                          const std::string &model_name,
                                          const std::string &log_prefix);

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

            // Check if config_file_ is a JSON string (starts with '{')
            std::string trimmed_cfg = self_->config_file_;
            size_t cfg_start = trimmed_cfg.find_first_not_of(" \t\n\r");
            if (cfg_start != std::string::npos)
            {
                trimmed_cfg = trimmed_cfg.substr(cfg_start);
            }

            bool is_json_string = (!trimmed_cfg.empty() && trimmed_cfg[0] == '{');

            if (is_json_string)
            {
                // config_file_ is a JSON string, not a file path
                return CreateIfVerifiedImpl();
            }

            // config_file_ is a file path, check if it exists
            if (File::IsFileExist(self_->config_file_) && !File::IsFileEmpty(self_->config_file_))
            {
                return CreateIfVerifiedImpl();
            }

            if (self_->known_model_path_.empty())
            {
                std::string err_str{"config file is not found: " + self_->config_file_};
                My_Log{My_Log::Level::kWarning} << err_str << std::endl;
                return nullptr;
            }

            std::string new_config_path{self_->known_model_path_ + "/config.json"};
            My_Log{My_Log::Level::kError} << "config file: " << self_->config_file_ << " "
                                          << "is not exist, will use default ver: " << new_config_path
                                          << std::endl;
            self_->config_file_ = new_config_path;

            return CreateIfVerifiedImpl();
        }

    protected:
        ModelInstanceConfig *config_;
        ModelManager *self_;
        bool config_strict_{true};
        const char *ext_{};

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
            std::string trimmed = self_->config_file_;
            size_t start = trimmed.find_first_not_of(" \t\n\r");
            if (start != std::string::npos)
            {
                trimmed = trimmed.substr(start);
            }

            if (!trimmed.empty() && trimmed[0] == '{')
            {
                // Parse JSON string directly
                try
                {
                    j = json::parse(self_->config_file_);
                } catch (const std::exception &e)
                {
                    return nullptr;
                }
            }
            else
            {
                std::ifstream file(self_->config_file_);
                if (!file.good())
                {
                    return nullptr;
                }
                file >> j;
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
                self_->context_size_ = context_size;
                My_Log{} << "fixed qnn context size: " << self_->context_size_ << "\n";
            }

            std::vector<std::string> files;
            std::string dtype_str;

            // TODO: use Regex embedding_weights_xxx.raw
            if (!File::MatchFileInDir(config_->get_model_path(), "embedding_weights", &files))
            {
                goto ahead;
            }

            My_Log{} << "check qnn embedding file: " << files[0] << "\n";
            if (File::IsFileEmpty(files[0]))
            {
                throw std::runtime_error("embedded file: " + files[0] + " is invalid");
            }

            static std::unordered_map<const char *, EmbeddingDataType> embedding_dtype_map{
                    {"float32", {EmbeddingDataType::FLOAT32}},
                    {"ufixed8", {EmbeddingDataType::INT8}},
            };

            self_->qnn_embedding_ = QNNImpl::TryCreate(config_->get_model_path(), files[0]);
            if (self_->qnn_embedding_.embedding_type_ == QNNEmbeddingType::None)
            {
                throw std::runtime_error("qnn model does not match any embedding rules");
            }
            My_Log{} << "check qnn embedding model type: " << self_->qnn_embedding_.model_types_.to_string() << "\n";

            if (!j.contains(json::json_pointer("/dialog/embedding/datatype")))
                throw std::runtime_error("qnn embedding has bad config in datatype");

            dtype_str = j.at(json::json_pointer("/dialog/embedding/datatype")).get_ref<const std::string &>();
            for (auto &item: embedding_dtype_map)
            {
                if (strcmp(item.first, dtype_str.c_str()) == 0)
                {
                    self_->qnn_embedding_.data_type = item.second;
                    break;
                }
            }

            if (self_->qnn_embedding_.data_type == EmbeddingDataType::None)
                throw std::runtime_error("qnn embedding has bad datatype");

            My_Log{} << "check qnn embedding data type: " << self_->qnn_embedding_.data_type.to_string() << "\n";
            ahead:
            config_->i_model_config_ = *self_;
            config_->i_model_config_.genieModelHandle = self_->genieModelHandle = std::make_shared<GenieContext>(*config_);
            return self_->genieModelHandle;
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
            // 增强：扣除同进程内已加载的其它模型（任意后端）的估算内存占用，
            // 降低多模型并发场景下预检查失效的概率（见 EstimateOtherLoadedModelsMemoryBytes 注释）。
            uint64_t other_models_reserved = self_->EstimateOtherLoadedModelsMemoryBytes();
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

            config_->i_model_config_ = *self_;
            config_->i_model_config_.genieModelHandle = self_->genieModelHandle = std::make_shared<MNNContext>(*config_);
            return self_->genieModelHandle;
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
            auto load_with_device = [this](const std::string &device) -> std::shared_ptr<ContextBase>
            {
                config_->set_device(device);
                config_->i_model_config_ = *self_;
                config_->i_model_config_.genieModelHandle = self_->genieModelHandle = std::make_shared<LLAMACppBuilder>(*config_);
                return self_->genieModelHandle;
            };

            std::string requested_device = config_->get_device();
            std::transform(requested_device.begin(), requested_device.end(), requested_device.begin(),
                           [](unsigned char c) { return static_cast<char>(std::tolower(c)); });

            // 模型自身 config.json 是可选的：缺失/为空/解析失败/不含 "backend" 键都静默忽略，
            // 只有明确写 {"backend": "cpu"} 时才强制走 CPU（与 service_config.json 的 device 是"或"关系）。
            try
            {
                json j;
                std::string trimmed = self_->config_file_;
                size_t start = trimmed.find_first_not_of(" \t\n\r");
                if (start != std::string::npos)
                {
                    trimmed = trimmed.substr(start);
                }

                if (!trimmed.empty() && trimmed[0] == '{')
                {
                    j = json::parse(self_->config_file_);
                }
                else if (File::IsFileExist(self_->config_file_) && !File::IsFileEmpty(self_->config_file_))
                {
                    std::ifstream file(self_->config_file_);
                    file >> j;
                }
                else if (!File::IsFileExist(self_->config_file_))
                {
                    // config.json 只允许精确匹配、不允许前缀匹配回退；GGUF 模型的 config.json 本身是可选的，
                    // 缺失时直接在模型目录下创建空文件占位，而不是借用其它模型的模板。
                    std::ofstream(self_->config_file_).close();
                }

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

            if (requested_device == "cpu")
            {
                return load_with_device("cpu");
            }

            try
            {
                return load_with_device("gpu");
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

        /* @formatter:off */
        std::vector<Checker> all_checkers{
                {{ModelFormat::QNN}, [config, manager](){ return QnnVerifier(config, manager).CreateIfVerified();}},
#ifdef USE_MNN
                {{ModelFormat::MNN}, [config, manager](){ return MnnVerifier(config, manager).CreateIfVerified();}},
#endif
#ifdef USE_GGUF
                {{ModelFormat::GGUF},[config, manager](){ return GGUFVerify(config, manager).CreateIfVerified();}}
#endif
        };
        /* @formatter:on */

        // 修复：若 config 中指定了 backend，优先只尝试该 backend，
        // 避免在多模型场景下（同一路径不同 backend/device）加载错误的后端。
        // 例如：同一 GGUF 模型路径可以用 CPU 或 GPU 后端加载，
        // 必须按 service_config.json 中的 backend 字段决定使用哪个后端。
        // 若指定的 backend 不在编译支持列表中，则回退到自动检测（向后兼容）。
        std::vector<Checker> checkers;
        const std::string &specified_backend = config->get_backend();
        if (!specified_backend.empty() && specified_backend != "auto")
        {
            // 将 backend 字符串映射到 ModelFormat
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
                // 只保留匹配 backend 的 checker
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
            // 未指定 backend 或 backend="auto"：自动检测（向后兼容）
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
    // 修复：显式析构，确保无论进程以何种方式退出（优雅关闭时 UnloadModel() 已清空 loaded_models_，
    // 这里只是空操作；但若通过测试框架/编排工具直接终止进程、或从未显式调用过 UnloadModel()，
    // 仍会残留驻留的 QNN/NPU 模型，届时只能靠这里的隐式成员析构去释放），都会在真正释放
    // 最后一份引用之前完成 QNN/HTP 驱动异步释放资源所需的等待。
    //
    // 根因（已通过 minidump 复现确认）：Clean() 里的等待只在 genieModelHandle 被置空、
    // 且此时确实是最后一份引用时才有意义；而编译器为 loaded_models_ 生成的隐式析构完全不会
    // 触发这段等待。此前该等待只存在于 UnloadModel()/UnloadModelsByDevice() 等显式调用路径，
    // atexit 析构链（`~ModelManager()` 从未显式定义、直接进入成员的隐式析构）完全没有保护，
    // 崩溃点位于释放 loaded_models_ 内部链表节点时（std::_List_node::_Free_non_head）。
    //
    // 修复方式：先在锁内清空 loaded_models_（记录其中是否含 QNN 后端条目），再处理
    // genieModelHandle——保证无论哪一份引用恰好是最后一份，其对应的等待都在这里完成，
    // 而不是被推迟到本函数返回之后的隐式成员析构阶段（那时已无法插入等待）。
    bool had_qnn = false;
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        for (const auto &pair: loaded_models_)
        {
            if (pair.second && pair.second->backend == "qnn")
            {
                had_qnn = true;
                break;
            }
        }
        loaded_models_.clear();
    }
    if (genieModelHandle != nullptr)
    {
        Clean();  // Clean() 内部已包含针对 QNN/NPU 的等待逻辑
    }
    else if (had_qnn)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(2000));
    }
}

bool ModelManager::LoadModelByName(const std::string &new_model, bool &first_load)
{
    loaded_ = false;
    if (new_model.empty())
    {
        My_Log{My_Log::Level::kError} << "model name can not be empty" << std::endl;
        return false;
    }

    if (model_name_ == new_model && genieModelHandle)
    {
        My_Log{} << "model: " + new_model << " has already been loaded" << std::endl;
        first_load = false;
        loaded_ = true;
        return true;
    }
    first_load = true;

    My_Log{} << "model name: " + new_model << " will be loaded" << std::endl;
    UpdateModeList();
    bool found{false};
    for (const auto &name: model_list_)
    {
        if (ModelComparer(name, new_model, false))
        {
            found = true;
            model_name_ = name;
            model_path_ = model_root_ + "/" + name;
            config_file_ = model_path_ + "/config.json";
            break;
        }
    }

    if (!found)
    {
        My_Log{My_Log::Level::kError} << "model name: " << new_model << " is not exist" << std::endl;
        return false;
    }

    return LoadSingleModel();
}

// ============================================================
// ParsePromptSectionsConfig: 从 prompt_optimization 配置对象中解析指定 key（如
// "prompt_sections" / "subagent_prompt_sections"）对应的段落过滤规则，填充到
// sections_cfg 中。InitializeConfig 中两处调用点共用此函数，避免重复代码。
// ============================================================
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

bool ModelManager::InitializeConfig(bool load)
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

    // 尝试加载 service_config.json 中的路由与云端配置
    {
        // 从程序根目录加载 service_config.json
        fs::path service_config_path = fs::path(RootDir) / "service_config.json";
        if (File::IsFileExist(service_config_path.generic_string()) &&
            !File::IsFileEmpty(service_config_path.generic_string()))
        {
            try
            {
                std::ifstream sc_file(service_config_path.generic_string());
                json sc_json;
                sc_file >> sc_json;

                // 优先加载 debug 配置（必须最先加载，避免后续节解析异常时 debug 配置未生效）
                // 历史问题：debug 节原来放在 try 块末尾，若前面任何节解析抛出异常，
                // debug 配置就永远不会被加载，导致 status_content_visible 保持默认值 true，
                // 状态消息文字（"Preparing inference..."等）会出现在 delta.content 中，
                // 进而被客户端写入历史消息，污染下一轮请求的 Prompt。
                if (sc_json.contains("debug"))
                {
                    const auto &dbg = sc_json["debug"];
                    ResponseTools::status_content_visible = dbg.value("status_update_content_visible", true);
                    routing_config_.sensitivity_detection.debug_log_matches = dbg.value("log_rule_matches", false);
                    ResponseTools::log_inference_stream = dbg.value("log_inference_stream", false);
                    My_Log{} << "Debug config loaded: status_update_content_visible="
                             << ResponseTools::status_content_visible
                             << ", log_rule_matches=" << routing_config_.sensitivity_detection.debug_log_matches
                             << ", log_inference_stream=" << ResponseTools::log_inference_stream
                             << std::endl;
                }

                // 加载 routing 配置
                if (sc_json.contains("routing"))
                {
                    const auto &r = sc_json["routing"];
                    routing_config_.enabled = r.value("enabled", false);
                    routing_config_.policy_id = r.value("policy_id", std::string("default_v1"));
                    routing_config_.prefer_local_for_simple = r.value("prefer_local_for_simple", true);
                    // 企业内网云是否要求对 S1 数据脱敏
                    // false（默认）= 企业云视为可信边界，S1 数据无需脱敏
                    // true         = 保守模式，S1 数据发往企业云前仍需脱敏
                    routing_config_.enterprise_cloud_require_desensitize =
                            r.value("enterprise_cloud_require_desensitize", false);

                    if (r.contains("sensitivity_detection"))
                    {
                        const auto &sd = r["sensitivity_detection"];
                        routing_config_.sensitivity_detection.enabled = sd.value("enabled", true);
                        routing_config_.sensitivity_detection.method = sd.value("method", std::string("rule_first"));
                        routing_config_.sensitivity_detection.use_local_model_fallback = sd.value("use_local_model_fallback", false);
                        routing_config_.sensitivity_detection.strict_s2_union = sd.value("strict_s2_union", true);
                        routing_config_.sensitivity_detection.timeout_ms = sd.value("timeout_ms", 300000);
                        routing_config_.sensitivity_detection.model_input_max_chars = sd.value("model_input_max_chars", 2000);
                        if (sd.contains("rule_level_overrides") && sd["rule_level_overrides"].is_object())
                        {
                            routing_config_.sensitivity_detection.rule_level_overrides.clear();
                            for (auto &[k, v]: sd["rule_level_overrides"].items())
                            {
                                if (v.is_string())
                                    routing_config_.sensitivity_detection.rule_level_overrides[k] = v.get<std::string>();
                            }
                        }
                        // 关键词词典路径：相对路径解析为相对于 RootDir（可执行文件目录）
                        {
                            std::string kw_path = sd.value("keywords_dict_path", std::string(""));
                            if (!kw_path.empty())
                            {
                                fs::path kw_fs_path(kw_path);
                                if (kw_fs_path.is_relative())
                                {
                                    // 相对路径：解析为相对于 RootDir（可执行文件所在目录）
                                    kw_path = (fs::path(RootDir) / kw_fs_path).generic_string();
                                }
                            }
                            routing_config_.sensitivity_detection.keywords_dict_path = kw_path;
                        }
                        routing_config_.sensitivity_detection.keywords_reload_interval_seconds = sd.value("keywords_reload_interval_seconds", 60);
                        // 安全检查最大生成 token 数（默认 2048，思考类模型需要更多 token）
                        routing_config_.sensitivity_detection.max_gen_tokens = sd.value("max_gen_tokens", 2048);
                        // 安全检测系统提示词（留空时使用内置默认值）
                        routing_config_.sensitivity_detection.system_prompt = sd.value("system_prompt", std::string(""));
                        // [扩展规则] 各类扩展检测规则的独立开关
                        if (sd.contains("extended_rules") && sd["extended_rules"].is_object())
                        {
                            const auto &er = sd["extended_rules"];
                            routing_config_.sensitivity_detection.extended_rules.enable_local_path = er.value("enable_local_path", true);
                            routing_config_.sensitivity_detection.extended_rules.enable_internal_url = er.value("enable_internal_url", true);
                            routing_config_.sensitivity_detection.extended_rules.enable_device_id = er.value("enable_device_id", true);
                            routing_config_.sensitivity_detection.extended_rules.enable_image_data = er.value("enable_image_data", true);
                            My_Log{} << "[Config] extended_rules loaded: "
                                     << "local_path="
                                     << routing_config_.sensitivity_detection.extended_rules.enable_local_path
                                     << ", internal_url="
                                     << routing_config_.sensitivity_detection.extended_rules.enable_internal_url
                                     << ", device_id="
                                     << routing_config_.sensitivity_detection.extended_rules.enable_device_id
                                     << ", image_data="
                                     << routing_config_.sensitivity_detection.extended_rules.enable_image_data
                                     << std::endl;
                        }
                        // [基础规则] 各类基础检测规则的独立开关
                        if (sd.contains("detection_rules") && sd["detection_rules"].is_object())
                        {
                            const auto &dr = sd["detection_rules"];
                            // 开关
                            routing_config_.sensitivity_detection.detection_rules.enable_phone = dr.value("enable_phone", true);
                            routing_config_.sensitivity_detection.detection_rules.enable_email = dr.value("enable_email", true);
                            routing_config_.sensitivity_detection.detection_rules.enable_id_card = dr.value("enable_id_card", true);
                            routing_config_.sensitivity_detection.detection_rules.enable_bank_card = dr.value("enable_bank_card", true);
                            routing_config_.sensitivity_detection.detection_rules.enable_api_key = dr.value("enable_api_key", true);
                            routing_config_.sensitivity_detection.detection_rules.enable_private_key = dr.value("enable_private_key", true);
                            routing_config_.sensitivity_detection.detection_rules.enable_token = dr.value("enable_token", true);
                            routing_config_.sensitivity_detection.detection_rules.enable_password = dr.value("enable_password", true);
                            // 各类型可配置敏感等级（S0/S1/S2），未配置时保持默认值
                            routing_config_.sensitivity_detection.detection_rules.level_phone = dr.value("level_phone", std::string("S1"));
                            routing_config_.sensitivity_detection.detection_rules.level_email = dr.value("level_email", std::string("S1"));
                            routing_config_.sensitivity_detection.detection_rules.level_id_card = dr.value("level_id_card", std::string("S2"));
                            routing_config_.sensitivity_detection.detection_rules.level_bank_card = dr.value("level_bank_card", std::string("S2"));
                            routing_config_.sensitivity_detection.detection_rules.level_api_key = dr.value("level_api_key", std::string("S2"));
                            routing_config_.sensitivity_detection.detection_rules.level_private_key = dr.value("level_private_key", std::string("S2"));
                            routing_config_.sensitivity_detection.detection_rules.level_token = dr.value("level_token", std::string("S2"));
                            routing_config_.sensitivity_detection.detection_rules.level_password = dr.value("level_password", std::string("S1"));
                            My_Log{} << "[Config] detection_rules loaded: "
                                     << "phone=" << routing_config_.sensitivity_detection.detection_rules.enable_phone
                                     << "(level=" << routing_config_.sensitivity_detection.detection_rules.level_phone
                                     << ")"
                                     << ", email=" << routing_config_.sensitivity_detection.detection_rules.enable_email
                                     << "(level=" << routing_config_.sensitivity_detection.detection_rules.level_email
                                     << ")"
                                     << ", id_card="
                                     << routing_config_.sensitivity_detection.detection_rules.enable_id_card
                                     << "(level=" << routing_config_.sensitivity_detection.detection_rules.level_id_card
                                     << ")"
                                     << ", bank_card="
                                     << routing_config_.sensitivity_detection.detection_rules.enable_bank_card
                                     << "(level="
                                     << routing_config_.sensitivity_detection.detection_rules.level_bank_card << ")"
                                     << ", api_key="
                                     << routing_config_.sensitivity_detection.detection_rules.enable_api_key
                                     << "(level=" << routing_config_.sensitivity_detection.detection_rules.level_api_key
                                     << ")"
                                     << ", private_key="
                                     << routing_config_.sensitivity_detection.detection_rules.enable_private_key
                                     << "(level="
                                     << routing_config_.sensitivity_detection.detection_rules.level_private_key << ")"
                                     << ", token=" << routing_config_.sensitivity_detection.detection_rules.enable_token
                                     << "(level=" << routing_config_.sensitivity_detection.detection_rules.level_token
                                     << ")"
                                     << ", password="
                                     << routing_config_.sensitivity_detection.detection_rules.enable_password
                                     << "(level="
                                     << routing_config_.sensitivity_detection.detection_rules.level_password << ")"
                                     << std::endl;
                        }
                    }

                    if (r.contains("desensitization"))
                    {
                        const auto &d = r["desensitization"];
                        routing_config_.desensitization.enabled = d.value("enabled", true);
                        if (d.contains("strategies") && d["strategies"].is_array())
                        {
                            routing_config_.desensitization.strategies.clear();
                            for (const auto &s: d["strategies"])
                                routing_config_.desensitization.strategies.push_back(s.get<std::string>());
                        }
                        routing_config_.desensitization.placeholder_style = d.value("placeholder_style", std::string("<{type}_{index}>"));
                        routing_config_.desensitization.iterative = d.value("iterative", false);
                        routing_config_.desensitization.max_rounds = d.value("max_rounds", 3);
                        routing_config_.desensitization.summarize_timeout_ms = d.value("summarize_timeout_ms", 300000);
                        // 摘要化脱敏系统提示词（留空时使用内置默认值）
                        routing_config_.desensitization.system_prompt = d.value("system_prompt", std::string(""));
                        routing_config_.desensitization.format_preserving_enabled = d.value("format_preserving_enabled", false);
                        routing_config_.desensitization.restore_response_enabled = d.value("restore_response_enabled", true);
                        routing_config_.desensitization.restore_stream_enabled = d.value("restore_stream_enabled", true);
                        routing_config_.desensitization.log_desensitization_details = d.value("log_desensitization_details", false);
                        // 各实体类型脱敏开关
                        if (d.contains("entity_switches") && d["entity_switches"].is_object())
                        {
                            const auto &es = d["entity_switches"];
                            routing_config_.desensitization.entity_switches.enable_phone = es.value("enable_phone", true);
                            routing_config_.desensitization.entity_switches.enable_email = es.value("enable_email", true);
                            routing_config_.desensitization.entity_switches.enable_id_card = es.value("enable_id_card", true);
                            routing_config_.desensitization.entity_switches.enable_bank_card = es.value("enable_bank_card", true);
                            routing_config_.desensitization.entity_switches.enable_api_key = es.value("enable_api_key", true);
                            routing_config_.desensitization.entity_switches.enable_private_key = es.value("enable_private_key", true);
                            routing_config_.desensitization.entity_switches.enable_token = es.value("enable_token", true);
                            routing_config_.desensitization.entity_switches.enable_password = es.value("enable_password", true);
                            routing_config_.desensitization.entity_switches.enable_internal_url = es.value("enable_internal_url", true);
                            routing_config_.desensitization.entity_switches.enable_local_path = es.value("enable_local_path", true);
                            routing_config_.desensitization.entity_switches.enable_device_id = es.value("enable_device_id", true);
                            routing_config_.desensitization.entity_switches.enable_image_data = es.value("enable_image_data", true);
                        }
                    }

                    if (r.contains("complexity"))
                    {
                        const auto &c = r["complexity"];
                        routing_config_.complexity.method = c.value("method", std::string("heuristic_first"));
                        routing_config_.complexity.use_local_model_fallback = c.value("use_local_model_fallback", false);
                        routing_config_.complexity.timeout_ms = c.value("timeout_ms", 300000);
                        routing_config_.complexity.model_input_max_chars = c.value("model_input_max_chars", 2000);
                        // 复杂度评估系统提示词（留空时使用内置默认值）
                        routing_config_.complexity.system_prompt = c.value("system_prompt", std::string(""));
                        if (c.contains("thresholds"))
                        {
                            const auto &t = c["thresholds"];
                            routing_config_.complexity.thresholds.tool_calls = t.value("tool_calls", 3);
                        }
                        // 复杂度关键词列表（可选，空列表时 CheckComplexKeywords 使用内置默认值）
                        if (c.contains("keywords_c1") && c["keywords_c1"].is_array())
                        {
                            routing_config_.complexity.keywords_c1.clear();
                            for (const auto &kw: c["keywords_c1"])
                            {
                                if (kw.is_string())
                                    routing_config_.complexity.keywords_c1.push_back(kw.get<std::string>());
                            }
                        }
                        if (c.contains("keywords_c2") && c["keywords_c2"].is_array())
                        {
                            routing_config_.complexity.keywords_c2.clear();
                            for (const auto &kw: c["keywords_c2"])
                            {
                                if (kw.is_string())
                                    routing_config_.complexity.keywords_c2.push_back(kw.get<std::string>());
                            }
                        }
                    }

                    if (r.contains("fallback"))
                    {
                        const auto &f = r["fallback"];
                        routing_config_.fallback.cloud_unavailable_to_local = f.value("cloud_unavailable_to_local", true);
                        // 支持两种 JSON 格式：扁平字段 或 local_unavailable 子对象
                        if (f.contains("local_unavailable") && f["local_unavailable"].is_object())
                        {
                            const auto &lu = f["local_unavailable"];
                            routing_config_.fallback.local_unavailable_s0 = lu.value("s0", std::string("cloud_if_allowed"));
                            routing_config_.fallback.local_unavailable_s1 = lu.value("s1", std::string("cloud_if_allowed"));
                            routing_config_.fallback.local_unavailable_s2 = lu.value("s2", std::string("fail"));
                        }
                        else
                        {
                            routing_config_.fallback.local_unavailable_s0 = f.value("local_unavailable_s0", std::string("cloud_if_allowed"));
                            routing_config_.fallback.local_unavailable_s1 = f.value("local_unavailable_s1", std::string("cloud_if_allowed"));
                            routing_config_.fallback.local_unavailable_s2 = f.value("local_unavailable_s2", std::string("fail"));
                        }
                        routing_config_.fallback.clean_local_history_on_fallback = f.value("clean_local_history_on_fallback", true);
                        // 本地输入溢出时云端可恢复错误的最大重试次数
                        routing_config_.fallback.max_input_overflow_retries = f.value("max_input_overflow_retries", 3);
                        // 企业云和公有云 fallback 策略
                        routing_config_.fallback.enterprise_cloud_unavailable =
                                f.value("enterprise_cloud_unavailable", std::string("public_cloud_if_allowed"));
                        routing_config_.fallback.public_cloud_unavailable =
                                f.value("public_cloud_unavailable", std::string("enterprise_cloud_if_allowed"));
                    }

                    if (r.contains("cache"))
                    {
                        const auto &ca = r["cache"];
                        routing_config_.cache.ttl_seconds = ca.value("ttl_seconds", 60);
                        routing_config_.cache.max_entries = ca.value("max_entries", 256);
                    }

                    if (r.contains("agent_routing"))
                    {
                        const auto &ar = r["agent_routing"];
                        routing_config_.agent_routing.sub_agent_prefer_local = ar.value("sub_agent_prefer_local", true);
                        routing_config_.agent_routing.sub_agent_allow_cloud_on_c2 = ar.value("sub_agent_allow_cloud_on_c2", true);
                        routing_config_.agent_routing.max_tool_call_retries = ar.value("max_tool_call_retries", 10);
                    }

                    // 会话级路由锁定配置
                    if (r.contains("sticky_routing"))
                    {
                        const auto &sr = r["sticky_routing"];
                        routing_config_.sticky_routing.enabled = sr.value("enabled", false);
                        routing_config_.sticky_routing.ttl_seconds = sr.value("ttl_seconds", 1800);
                        routing_config_.sticky_routing.max_sessions = sr.value("max_sessions", 1000);
                    }

                    // 指标汇总输出配置
                    if (r.contains("metrics"))
                    {
                        const auto &m = r["metrics"];
                        routing_config_.metrics.summary_every_n_requests = m.value("summary_every_n_requests", 100);
                        routing_config_.metrics.summary_every_seconds = m.value("summary_every_seconds", 0);
                        routing_config_.metrics.latency_sample_size = m.value("latency_sample_size", 1000);
                        routing_config_.metrics.fail_reason_topn = m.value("fail_reason_topn", 5);
                    }

                    // [增量检查优化] 安全检查增量模式配置
                    if (r.contains("incremental_check"))
                    {
                        const auto &ic = r["incremental_check"];
                        routing_config_.incremental_check.enabled = ic.value("enabled", false);
                        routing_config_.incremental_check.session_ttl_seconds = ic.value("session_ttl_seconds", 3600);
                        routing_config_.incremental_check.max_sessions = ic.value("max_sessions", 1000);
                        routing_config_.incremental_check.s2_always_full_check = ic.value("s2_always_full_check", true);
                        routing_config_.incremental_check.detect_sensitive_reference = ic.value("detect_sensitive_reference", true);
                        routing_config_.incremental_check.detect_history_tampering = ic.value("detect_history_tampering", true);
                        My_Log{} << "Incremental check config loaded: enabled="
                                 << routing_config_.incremental_check.enabled
                                 << ", session_ttl_seconds=" << routing_config_.incremental_check.session_ttl_seconds
                                 << ", max_sessions=" << routing_config_.incremental_check.max_sessions << std::endl;
                    }

                    // [S2 轮次清理] S2 轮次清理配置
                    if (r.contains("s2_turn_cleaning"))
                    {
                        const auto &stc = r["s2_turn_cleaning"];
                        routing_config_.s2_turn_cleaning.enabled = stc.value("enabled", true);
                        routing_config_.s2_turn_cleaning.log_details = stc.value("log_details", true);
                        routing_config_.s2_turn_cleaning.allow_cloud_reroute_after_clean = stc.value("allow_cloud_reroute_after_clean", false);
                        My_Log{} << "S2 turn cleaning config loaded: enabled="
                                 << routing_config_.s2_turn_cleaning.enabled
                                 << ", log_details=" << routing_config_.s2_turn_cleaning.log_details
                                 << ", allow_cloud_reroute="
                                 << routing_config_.s2_turn_cleaning.allow_cloud_reroute_after_clean << std::endl;
                    }

                    My_Log{} << "Routing config loaded: enabled=" << routing_config_.enabled
                             << ", policy_id=" << routing_config_.policy_id << std::endl;
                }

                // ── [cloud_shared] 加载共享配置（timeout/stream_timeout/log_debug/retry/circuit_breaker/rate_limit）
                // 新版 service_config.json 将这些字段提取到 cloud_shared 节，
                // cloud_model 和 enterprise_cloud_model 只保留各自独有字段，共享字段从此处继承。
                // 向后兼容：若 cloud_shared 不存在，各字段保持结构体默认值。
                int shared_timeout_seconds = 120;
                int shared_stream_timeout_seconds = 600;
                bool shared_log_debug = false;
                int shared_retry_max = 2;
                int shared_retry_backoff_ms = 200;
                int shared_retry_max_total_attempts = 0;
                bool shared_retry_on_429_switch_endpoint = false;
                int shared_cb_failure_threshold = 3;
                int shared_cb_cooldown_seconds = 60;
                int shared_rl_max_inferences = 20;
                int shared_rl_max_tokens = 0;

                if (sc_json.contains("cloud_shared"))
                {
                    const auto &cs = sc_json["cloud_shared"];
                    shared_timeout_seconds = cs.value("timeout_seconds", shared_timeout_seconds);
                    shared_stream_timeout_seconds = cs.value("stream_timeout_seconds", shared_stream_timeout_seconds);
                    shared_log_debug = cs.value("log_debug", shared_log_debug);
                    if (cs.contains("retry"))
                    {
                        const auto &ret = cs["retry"];
                        shared_retry_max = ret.value("max", shared_retry_max);
                        shared_retry_backoff_ms = ret.value("backoff_ms", shared_retry_backoff_ms);
                        shared_retry_max_total_attempts = ret.value("max_total_attempts", shared_retry_max_total_attempts);
                        shared_retry_on_429_switch_endpoint = ret.value("retry_on_429_switch_endpoint", shared_retry_on_429_switch_endpoint);
                    }
                    if (cs.contains("circuit_breaker"))
                    {
                        const auto &cb = cs["circuit_breaker"];
                        shared_cb_failure_threshold = cb.value("failure_threshold", shared_cb_failure_threshold);
                        shared_cb_cooldown_seconds = cb.value("cooldown_seconds", shared_cb_cooldown_seconds);
                    }
                    if (cs.contains("rate_limit"))
                    {
                        const auto &rl = cs["rate_limit"];
                        shared_rl_max_inferences = rl.value("max_inferences_per_task", shared_rl_max_inferences);
                        shared_rl_max_tokens = rl.value("max_tokens_per_task", shared_rl_max_tokens);
                    }
                    My_Log{} << "[cloud_shared] Loaded: timeout=" << shared_timeout_seconds
                             << ", stream_timeout=" << shared_stream_timeout_seconds
                             << ", retry.max=" << shared_retry_max
                             << ", rate_limit.max_inferences=" << shared_rl_max_inferences
                             << std::endl;
                }

                // 加载 cloud_model 配置
                // 共享字段先从 cloud_shared 继承，cloud_model 节中若存在同名字段则覆盖（向后兼容旧格式）
                if (sc_json.contains("cloud_model"))
                {
                    const auto &cm = sc_json["cloud_model"];
                    cloud_model_config_.enabled = cm.value("enabled", false);
                    cloud_model_config_.base_url = cm.value("base_url", std::string(""));
                    // API Key：优先从 Windows Credential Manager 读取，fallback 到 JSON 值
                    cloud_model_config_.api_key = ResolveApiKey(
                            cm.value("api_key", std::string("")), "global::cloud_model");
                    cloud_model_config_.model = cm.value("model", std::string(""));
                    // 共享字段：优先使用 cloud_model 节中的值（向后兼容），否则使用 cloud_shared 的值
                    cloud_model_config_.timeout_seconds = cm.value("timeout_seconds", shared_timeout_seconds);
                    cloud_model_config_.stream_timeout_seconds = cm.value("stream_timeout_seconds", shared_stream_timeout_seconds);
                    cloud_model_config_.log_debug = cm.value("log_debug", shared_log_debug);

                    if (cm.contains("retry"))
                    {
                        const auto &ret = cm["retry"];
                        cloud_model_config_.retry.max = ret.value("max", shared_retry_max);
                        cloud_model_config_.retry.backoff_ms = ret.value("backoff_ms", shared_retry_backoff_ms);
                        cloud_model_config_.retry.max_total_attempts = ret.value("max_total_attempts", shared_retry_max_total_attempts);
                        cloud_model_config_.retry.retry_on_429_switch_endpoint = ret.value("retry_on_429_switch_endpoint", shared_retry_on_429_switch_endpoint);
                    }
                    else
                    {
                        // 无 retry 子节：使用 cloud_shared 的值
                        cloud_model_config_.retry.max = shared_retry_max;
                        cloud_model_config_.retry.backoff_ms = shared_retry_backoff_ms;
                        cloud_model_config_.retry.max_total_attempts = shared_retry_max_total_attempts;
                        cloud_model_config_.retry.retry_on_429_switch_endpoint = shared_retry_on_429_switch_endpoint;
                    }

                    if (cm.contains("circuit_breaker"))
                    {
                        const auto &cb = cm["circuit_breaker"];
                        cloud_model_config_.circuit_breaker.failure_threshold = cb.value("failure_threshold", shared_cb_failure_threshold);
                        cloud_model_config_.circuit_breaker.cooldown_seconds = cb.value("cooldown_seconds", shared_cb_cooldown_seconds);
                    }
                    else
                    {
                        cloud_model_config_.circuit_breaker.failure_threshold = shared_cb_failure_threshold;
                        cloud_model_config_.circuit_breaker.cooldown_seconds = shared_cb_cooldown_seconds;
                    }

                    if (cm.contains("endpoints") && cm["endpoints"].is_array())
                    {
                        cloud_model_config_.endpoints.clear();
                        for (const auto &ep: cm["endpoints"])
                        {
                            CloudModelConfig::Endpoint endpoint;
                            endpoint.name = ep.value("name", std::string(""));
                            endpoint.base_url = ep.value("base_url", std::string(""));
                            endpoint.model = ep.value("model", std::string(""));
                            cloud_model_config_.endpoints.push_back(endpoint);
                        }
                    }

                    if (cm.contains("rate_limit"))
                    {
                        const auto &rl = cm["rate_limit"];
                        cloud_model_config_.rate_limit.max_inferences_per_task = rl.value("max_inferences_per_task", shared_rl_max_inferences);
                        cloud_model_config_.rate_limit.max_tokens_per_task = rl.value("max_tokens_per_task", shared_rl_max_tokens);
                    }
                    else
                    {
                        cloud_model_config_.rate_limit.max_inferences_per_task = shared_rl_max_inferences;
                        cloud_model_config_.rate_limit.max_tokens_per_task = shared_rl_max_tokens;
                    }

                    // 加载数据上云策略配置（仅云端模式下生效）
                    if (cm.contains("upload_policy"))
                    {
                        const auto &up = cm["upload_policy"];
                        cloud_model_config_.upload_policy.enable_sensitivity_check =
                                up.value("enable_sensitivity_check", true);
                        cloud_model_config_.upload_policy.enable_desensitization =
                                up.value("enable_desensitization", true);
                    }

                    // 加载 context_size（用于统一提示词优化流水线的 token 预算计算）
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

                // 加载 enterprise_cloud_model 配置
                // 共享字段先从 cloud_shared 继承，enterprise_cloud_model 节中若存在同名字段则覆盖（向后兼容旧格式）
                if (sc_json.contains("enterprise_cloud_model"))
                {
                    const auto &ecm = sc_json["enterprise_cloud_model"];
                    enterprise_cloud_model_config_.enabled = ecm.value("enabled", false);
                    enterprise_cloud_model_config_.base_url = ecm.value("base_url", std::string(""));
                    // API Key：优先从 Windows Credential Manager 读取，fallback 到 JSON 值
                    enterprise_cloud_model_config_.api_key = ResolveApiKey(
                            ecm.value("api_key", std::string("")), "global::enterprise_cloud_model");
                    enterprise_cloud_model_config_.model = ecm.value("model", std::string(""));
                    // 共享字段：优先使用 enterprise_cloud_model 节中的值（向后兼容），否则使用 cloud_shared 的值
                    enterprise_cloud_model_config_.timeout_seconds = ecm.value("timeout_seconds", shared_timeout_seconds);
                    enterprise_cloud_model_config_.stream_timeout_seconds = ecm.value("stream_timeout_seconds", shared_stream_timeout_seconds);
                    enterprise_cloud_model_config_.log_debug = ecm.value("log_debug", shared_log_debug);

                    if (ecm.contains("retry"))
                    {
                        const auto &ret = ecm["retry"];
                        enterprise_cloud_model_config_.retry.max = ret.value("max", shared_retry_max);
                        enterprise_cloud_model_config_.retry.backoff_ms = ret.value("backoff_ms", shared_retry_backoff_ms);
                        enterprise_cloud_model_config_.retry.max_total_attempts = ret.value("max_total_attempts", shared_retry_max_total_attempts);
                        enterprise_cloud_model_config_.retry.retry_on_429_switch_endpoint = ret.value("retry_on_429_switch_endpoint", shared_retry_on_429_switch_endpoint);
                    }
                    else
                    {
                        // 无 retry 子节：使用 cloud_shared 的值
                        enterprise_cloud_model_config_.retry.max = shared_retry_max;
                        enterprise_cloud_model_config_.retry.backoff_ms = shared_retry_backoff_ms;
                        enterprise_cloud_model_config_.retry.max_total_attempts = shared_retry_max_total_attempts;
                        enterprise_cloud_model_config_.retry.retry_on_429_switch_endpoint = shared_retry_on_429_switch_endpoint;
                    }

                    if (ecm.contains("circuit_breaker"))
                    {
                        const auto &cb = ecm["circuit_breaker"];
                        enterprise_cloud_model_config_.circuit_breaker.failure_threshold = cb.value("failure_threshold", shared_cb_failure_threshold);
                        enterprise_cloud_model_config_.circuit_breaker.cooldown_seconds = cb.value("cooldown_seconds", shared_cb_cooldown_seconds);
                    }
                    else
                    {
                        enterprise_cloud_model_config_.circuit_breaker.failure_threshold = shared_cb_failure_threshold;
                        enterprise_cloud_model_config_.circuit_breaker.cooldown_seconds = shared_cb_cooldown_seconds;
                    }

                    if (ecm.contains("endpoints") && ecm["endpoints"].is_array())
                    {
                        enterprise_cloud_model_config_.endpoints.clear();
                        for (const auto &ep: ecm["endpoints"])
                        {
                            EnterpriseCloudModelConfig::Endpoint endpoint;
                            endpoint.name = ep.value("name", std::string(""));
                            endpoint.base_url = ep.value("base_url", std::string(""));
                            endpoint.model = ep.value("model", std::string(""));
                            enterprise_cloud_model_config_.endpoints.push_back(endpoint);
                        }
                    }

                    if (ecm.contains("rate_limit"))
                    {
                        const auto &rl = ecm["rate_limit"];
                        enterprise_cloud_model_config_.rate_limit.max_inferences_per_task = rl.value("max_inferences_per_task", shared_rl_max_inferences);
                        enterprise_cloud_model_config_.rate_limit.max_tokens_per_task = rl.value("max_tokens_per_task", shared_rl_max_tokens);
                    }
                    else
                    {
                        enterprise_cloud_model_config_.rate_limit.max_inferences_per_task = shared_rl_max_inferences;
                        enterprise_cloud_model_config_.rate_limit.max_tokens_per_task = shared_rl_max_tokens;
                    }

                    // 加载 context_size（用于统一提示词优化流水线的 token 预算计算）
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

                // 加载 local_model 配置
                if (sc_json.contains("local_model"))
                {
                    const auto &lm = sc_json["local_model"];
                    local_model_config_.enabled = lm.value("enabled", true);

                    My_Log{} << "Local model config loaded: enabled=" << local_model_config_.enabled
                             << std::endl;
                }

                // 加载 prompt_optimization 配置
                if (sc_json.contains("prompt_optimization"))
                {
                    const auto &po = sc_json["prompt_optimization"];
                    prompt_optimization_config_.output_reserve_ratio = po.value("output_reserve_ratio", 0.20f);
                    prompt_optimization_config_.max_messages_limit = po.value("max_messages_limit", (size_t) 16);
                    prompt_optimization_config_.recent_window = po.value("recent_window", (size_t) 6);
                    prompt_optimization_config_.old_compress_len = po.value("old_compress_len", (size_t) 300);
                    prompt_optimization_config_.recent_compress_len = po.value("recent_compress_len", (size_t) 600);
                    prompt_optimization_config_.tool_compress_len = po.value("tool_compress_len", (size_t) 400);
                    prompt_optimization_config_.min_compress_threshold = po.value("min_compress_threshold", (size_t) 10);
                    prompt_optimization_config_.tool_min_length = po.value("tool_min_length", (size_t) 300);

                    // 紧急截断配置
                    if (po.contains("emergency_truncation") && po["emergency_truncation"].is_object())
                    {
                        const auto &et = po["emergency_truncation"];
                        prompt_optimization_config_.emergency_truncation.enabled =
                                et.value("enabled", true);
                        prompt_optimization_config_.emergency_truncation.max_truncation_ratio =
                                et.value("max_truncation_ratio", 0.40f);
                        prompt_optimization_config_.emergency_truncation.safety_margin_tokens =
                                et.value("safety_margin_tokens", 30);
                        My_Log{} << "[Config] emergency_truncation loaded: "
                                 << "enabled=" << prompt_optimization_config_.emergency_truncation.enabled
                                 << ", max_truncation_ratio="
                                 << prompt_optimization_config_.emergency_truncation.max_truncation_ratio
                                 << ", safety_margin_tokens="
                                 << prompt_optimization_config_.emergency_truncation.safety_margin_tokens
                                 << std::endl;
                    }

                    // [system_context] 系统上下文配置（所有系统提示词内容均从此处读取）
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

                    // [prompt_sections] 原始系统提示词段落过滤配置
                    ParsePromptSectionsConfig(po, prompt_optimization_config_.prompt_sections, "prompt_sections");

                    // [subagent_prompt_sections] SubAgent 专用段落过滤配置
                    ParsePromptSectionsConfig(po, prompt_optimization_config_.subagent_prompt_sections, "subagent_prompt_sections");

                    prompt_optimization_config_.skill_catalog_format = po.value("skill_catalog_format", "structured");
                    prompt_optimization_config_.enable_tool_whitelist = po.value("enable_tool_whitelist", true);
                    prompt_optimization_config_.enable_skill_auto_correction = po.value("enable_skill_auto_correction", true);
                    prompt_optimization_config_.tool_call_temperature = po.value("tool_call_temperature", 0.1f);

                    if (po.contains("allowed_tools") && po["allowed_tools"].is_array())
                    {
                        prompt_optimization_config_.allowed_tools.clear();
                        for (const auto &tool: po["allowed_tools"])
                        {
                            prompt_optimization_config_.allowed_tools.push_back(tool.get<std::string>());
                        }
                    }

                    if (po.contains("system_prompts"))
                    {
                        const auto &sp = po["system_prompts"];
                        prompt_optimization_config_.system_prompts.identity_intro = sp.value("identity_intro", "");
                        prompt_optimization_config_.system_prompts.skill_rule = sp.value("skill_rule", "");
                        prompt_optimization_config_.system_prompts.tools_intro = sp.value("tools_intro", "");
                        prompt_optimization_config_.system_prompts.catalog_structured_intro = sp.value("catalog_structured_intro", "");

                        // ── 各静态段落的启用开关（有默认值 true，配置文件中可选覆盖）──
                        if (sp.contains("sections_enabled") && sp["sections_enabled"].is_object())
                        {
                            const auto &se = sp["sections_enabled"];
                            prompt_optimization_config_.system_prompts.sections_enabled.identity_intro = se.value("identity_intro", true);
                            prompt_optimization_config_.system_prompts.sections_enabled.skill_rule = se.value("skill_rule", true);
                            prompt_optimization_config_.system_prompts.sections_enabled.tools_intro = se.value("tools_intro", true);
                            prompt_optimization_config_.system_prompts.sections_enabled.catalog_structured_intro = se.value("catalog_structured_intro", true);
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

                        // ── Few-shot 示例各类型的启用开关 ──────────────────────────────
                        if (sp.contains("few_shot_examples_enabled") && sp["few_shot_examples_enabled"].is_object())
                        {
                            const auto &fe = sp["few_shot_examples_enabled"];
                            prompt_optimization_config_.system_prompts.few_shot_examples_enabled.enabled = fe.value("enabled", true);
                            prompt_optimization_config_.system_prompts.few_shot_examples_enabled.skill_correct_call = fe.value("skill_correct_call", true);
                            prompt_optimization_config_.system_prompts.few_shot_examples_enabled.no_skill_needed = fe.value("no_skill_needed", true);
                            // max_skill_examples：最多生成几个 Skill 示例（0=不生成，1=只生成第1个，2=前2个，默认2）
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

                        // few_shot_header：有默认值，配置文件中可选覆盖
                        if (sp.contains("few_shot_header"))
                            prompt_optimization_config_.system_prompts.few_shot_header = sp.value("few_shot_header", "## Examples\n\n");
                        // few-shot dynamic example templates: have defaults, optionally overridden by config file
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

                    // 加载 spawn_guard 配置
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

                    // 加载 long_text_summarization 配置
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

                        // 加载缓存子配置
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

                        // 配置加载完成后立即同步缓存配置
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

                    My_Log{} << "Prompt optimization config loaded: "
                             << "output_reserve_ratio=" << prompt_optimization_config_.output_reserve_ratio
                             << ", recent_window=" << prompt_optimization_config_.recent_window
                             << ", old_compress_len=" << prompt_optimization_config_.old_compress_len
                             << ", recent_compress_len=" << prompt_optimization_config_.recent_compress_len
                             << ", format=" << prompt_optimization_config_.skill_catalog_format
                             << ", whitelist=" << prompt_optimization_config_.enable_tool_whitelist
                             << ", auto_correction=" << prompt_optimization_config_.enable_skill_auto_correction
                             << std::endl;
                }

                startup_backend_override_.clear();
                startup_device_override_.clear();
                startup_context_size_override_ = 0;
                if (sc_json.contains("models") && sc_json["models"].is_array())
                {
                    for (const auto &m: sc_json["models"])
                    {
                        std::string name = m.value("name", "");
                        std::string path = m.value("path", "");
                        if (name == model_name_ || fs::path(path).filename().generic_string() == model_name_)
                        {
                            startup_backend_override_ = m.value("backend", std::string(""));
                            startup_device_override_ = m.value("device", std::string(""));
                            startup_context_size_override_ = m.value("context_size", 0);
                            My_Log{} << "[InitializeConfig] service_config startup override for model '"
                                     << model_name_ << "': backend=" << startup_backend_override_
                                     << ", device=" << startup_device_override_
                                     << ", context_size=" << startup_context_size_override_ << std::endl;
                            break;
                        }
                    }
                }

            }
            catch (const std::exception &e)
            {
                My_Log{My_Log::Level::kError} << "Failed to load service_config.json: " << e.what() << std::endl;
                // 配置加载失败不影响主流程，使用默认值
            }
        }
    }

    if (!load)
        return true;

    return LoadSingleModel();
}

bool ModelManager::LoadSingleModel()
{
    // 修复：在多模型场景下，Clean() 会清空 genieModelHandle 和 qnn_embedding_，
    // 但不会影响 loaded_models_ 中其他模型的 shared_ptr（它们是独立的引用计数）。
    // 然而，Clean() 会将 genieModelHandle 置为 nullptr，这会导致 loaded_models_ 中
    // 已注册的旧单模型条目（通过 LoadSingleModel 注册的）失去全局句柄引用，
    // 但 loaded_models_ 中的 shared_ptr 仍然有效（引用计数不为零）。
    // 因此，在调用 Clean() 之前，先从 loaded_models_ 中移除旧的单模型条目，
    // 避免 loaded_models_ 中存在指向已销毁上下文的悬空条目。
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        if (!model_name_.empty() && loaded_models_.count(model_name_))
        {
            My_Log{} << "LoadSingleModel: removing old single-model entry '" << model_name_
                     << "' from loaded_models_ before reload" << std::endl;
            loaded_models_.erase(model_name_);
        }
    }

    Clean();
    prompt_type_ = LoadPromptTemplates(model_path_ + "/prompt.json");
    if (prompt_type_ == PromptType::Unknown)
        return false;
    My_Log{} << "check the prompt type: " << prompt_type_.to_string() << "\n";

    thinking_model_ = [this]() -> bool
    {
        return str_contains(model_name_, "Qwen3") ||
               str_contains(model_name_, "DeepSeek") ||
               str_contains(model_name_, "Hunyuan");
    }();
    My_Log{} << "check if is thinking model: " << thinking_model_ << "\n";

    // Create a temporary ModelInstanceConfig for backward compatibility
    auto config = std::make_shared<ModelInstanceConfig>();
    config->set_model_name(model_name_);
    config->set_model_path(model_path_);
    config->set_context_size(context_size_);
    config->set_prompt_template(prompt_);
    config->set_prompt_type(prompt_type_);
    config->set_thinking_model(thinking_model_);
    // 单模型命令行加载路径默认保持旧版本的“自动识别模型格式”行为。
    // 若 service_config.json 中存在与 -c config.json 对应模型匹配的条目，则使用服务级 backend/device 覆盖项。
    if (!startup_backend_override_.empty())
    {
        config->set_backend(startup_backend_override_);
    }
    else
    {
        config->set_backend("auto");
    }
    if (!startup_device_override_.empty())
    {
        config->set_device(startup_device_override_);
    }
    if (startup_context_size_override_ > 0)
    {
        config->set_context_size(startup_context_size_override_);
    }
    // Copy other config fields as needed
    config->set_lora_adapter(loraAdapter);
    config->set_lora_alpha(loraAlpha);
    config->set_output_all_text(outputAllText);
    config->set_enable_thinking(enableThinking);
    config->set_enable_prompt_debug(enablePromptDebug);
    config->set_num_response(num_response_);
    config->set_min_output_num(minOutputNum);

    genieModelHandle = ModeVerifier::TryCreate(config.get(), this);
    if (!genieModelHandle)
    {
        My_Log{My_Log::Level::kError} << RED
                                      << "Load Model Failed"
                                      << ", Model Name: " << model_name_
                                      << ", Model Path: " << model_path_
                                      << RESET << std::endl;
        model_name_.clear();
        return false;
    }

    // Sync back properties determined during creation
    model_format_ = config->get_model_format();

    // 修复：根据实际检测到的模型格式推断 backend 和 device，
    // 而非使用 ModelInstanceConfig 的默认值（"GGUF"/"gpu"）。
    // LoadSingleModel() 通过 ModeVerifier::TryCreate() 自动检测模型格式，
    // 需要将检测结果同步回 config 的 backend/device 字段。
    {
        ModelFormat fmt = config->get_model_format();
        if (fmt == ModelFormat::QNN)
        {
            // QNN 模型只能跑在 NPU 上
            config->set_backend("qnn");
            config->set_device("npu");
        }
        else if (fmt == ModelFormat::MNN)
        {
            // MNN 模型跑在 CPU 上
            config->set_backend("mnn");
            config->set_device("cpu");
        }
        else if (fmt == ModelFormat::GGUF)
        {
            // GGUF 模型通过 llama.cpp 后端运行，优先 GPU，可 fallback CPU。
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

    // Also register this as the default model in the new system
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        auto loaded_model = std::make_shared<LoadedModel>();
        loaded_model->config = config;
        loaded_model->context = genieModelHandle;
        loaded_model->backend = config->get_backend();
        loaded_model->device = config->get_device();
        loaded_model->is_loaded = true;

        // Use the model name as key
        loaded_models_[model_name_] = loaded_model;
        default_model_name_ = model_name_;
    }

    My_Log{} << GREEN << "Model load successfully: " << model_name_ << RESET << std::endl;
    loaded_ = true;
    return true;

}

void ModelManager::UnloadModel()
{
    // 主动信号任何在飞的 Query() 调用尽快退出：在真正清空引用/触发析构之前，对每个
    // 已加载模型的 context 调用一次 Stop()。这是对既有 /reload 类接口（ModelStop）
    // 已在用的能力的复用，让优雅关闭路径（GenieService::ServiceStop）能尽量缩短等待
    // 时间——尤其是 MNN 后端在内存压力下 generate() 调用可能长时间阻塞的场景，Stop()
    // 让其生成循环在下一次检查 m_stop 时尽快退出，而不是从未被通知过。
    if (genieModelHandle != nullptr)
    {
        genieModelHandle->Stop();
    }
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        for (const auto &pair: loaded_models_)
        {
            if (pair.second && pair.second->context)
            {
                pair.second->context->Stop();
            }
        }
    }

    // 清理全局单模型句柄（向后兼容路径）
    if (genieModelHandle != nullptr)
    {
        Clean();
        model_name_.clear();
    }
    else
    {
        My_Log{My_Log::Level::kWarning} << "UnloadModel: genieModelHandle is null (may be multi-model mode)"
                                        << std::endl;
    }

    // 同时清理多模型映射表，确保多模型场景下资源完整释放
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        if (!loaded_models_.empty())
        {
            My_Log{} << "UnloadModel: clearing " << loaded_models_.size()
                     << " loaded model(s) from multi-model registry" << std::endl;
            loaded_models_.clear();
            default_model_name_.clear();
        }
    }
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

        std::string config_path = entry.path().generic_string() + "/config.json";
        if (!File::IsFileExist(config_path))
            continue;

        std::string name = entry.path().filename().generic_string();
        json m;
        m["id"] = name;

        int ctx = 0;
        std::string backend = "unknown";
        std::string device = "unknown";

        // 从 config.json 推断格式和 context_size
        // 空文件是 GGUF 模型的正常情况（仅作目录标识），直接跳过解析
        if (File::IsFileEmpty(config_path))
        {
            backend = "GGUF";
            device = "gpu";
        }
        else
        {
            // 优先检查目录内是否存在 *.gguf 权重文件：这是比 config.json 字段更可靠的判据，
            // 因为部分 GGUF 模型目录沿用了 MNN 风格的占位 config.json（含 llm_model 字段），
            // 若仅凭字段判断会被误判为 MNN 格式，导致按错误后端加载失败。
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
                        // QNN / SSD 格式：dialog.context.size
                        ctx = cfg["dialog"]["context"].value("size", 0);
                        backend = "qnn";
                        device = "npu";
                    }
                    else if (cfg.contains("llm_model"))
                    {
                        // MNN 格式
                        ctx = cfg.value("context_size", cfg.value("context_length", 0));
                        backend = "mnn";
                        device = "cpu";
                    }
                    else
                    {
                        // GGUF 格式（config.json 存在但无已知格式标识字段）
                        backend = "GGUF";
                        device = "gpu";
                    }
                }
                catch (const std::exception &e)
                {
                    My_Log{My_Log::Level::kWarning}
                            << "[ScanModelDirectory] Failed to parse config.json for model '" << name
                            << "': " << e.what() << std::endl;
                    // 解析失败时保守地视为 GGUF 格式
                    backend = "GGUF";
                    device = "gpu";
                }
            }
        }

        // 若 config.json 未提供 context_size，尝试 prompt.json
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

void ModelManager::UnloadModelsByDevice(const std::string &device)
{
    std::vector<std::string> to_remove;
    // 用于在锁外持有被移除模型的 shared_ptr，确保析构在锁外发生，
    // 同时保证在本函数返回前析构完成（NPU/GPU/CPU 内存完全释放后再允许加载新模型）。
    std::vector<std::shared_ptr<LoadedModel>> removed_models;
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        for (const auto &kv: loaded_models_)
        {
            if (kv.second && kv.second->device == device)
                to_remove.push_back(kv.first);
        }
        for (const auto &name: to_remove)
        {
            My_Log{} << "[UnloadModelsByDevice] Unloading model '" << name
                     << "' (device=" << device << ")" << std::endl;
            auto it = loaded_models_.find(name);
            if (it != loaded_models_.end())
            {
                // 先将 shared_ptr 移出到局部列表，再从 map 中 erase，
                // 这样析构会在锁外、函数返回前发生，避免持锁析构死锁。
                removed_models.push_back(std::move(it->second));
                loaded_models_.erase(it);
            }
            if (default_model_name_ == name)
                default_model_name_.clear();
        }
    }
    // 若被卸载的模型中包含当前全局单模型句柄所指向的模型，同步清理（向后兼容路径）
    // 不限于 npu：GGUF 模型也可能通过 LoadSingleModel 注册到全局句柄
    if (genieModelHandle != nullptr)
    {
        bool should_clean = false;
        if (device == "npu")
        {
            should_clean = true;  // NPU 模型必然是 QNN，全局句柄指向 QNN 模型
        }
        else
        {
            // 检查全局句柄对应的模型名称是否在被卸载列表中
            for (const auto &name: to_remove)
            {
                if (name == model_name_)
                {
                    should_clean = true;
                    break;
                }
            }
        }
        if (should_clean)
        {
            // Clean() 将 genieModelHandle 置为 nullptr，减少引用计数。
            // 若 genieModelHandle 是最后一个持有者，GenieContext 在此处析构。
            Clean();
            model_name_.clear();
        }
    }
    // 在锁外、函数返回前，显式析构所有被移除的模型（释放硬件资源）。
    // removed_models 超出作用域时，其中每个 shared_ptr 的引用计数降为零，
    // 触发 GenieContext/MNNContext/LLAMACppBuilder 析构，完全释放 NPU/GPU/CPU 内存。
    // 这保证了调用方在本函数返回后可以安全地加载新模型，不会出现内存不足。
    //
    // 修复：这里才是 removed_models 中每个模型真正的最后一份引用被释放、~GenieContext()
    // 真正执行的时刻（上面第2079~2105行 Clean() 归零的只是 genieModelHandle 这一份引用，
    // 若该模型同时也在 removed_models 中，此时引用计数还没到 0）。QNN/HTP 驱动异步释放资源
    // 需要显式等待才安全，此前这里完全没有等待保护——这是与 Clean() 处已确认的竞态型堆损坏
    // （STATUS_HEAP_CORRUPTION）同一缺陷模式在多模型动态切换路径上的重复，需要同等对待。
    bool had_qnn_backend = std::any_of(removed_models.begin(), removed_models.end(),
                                        [](const std::shared_ptr<LoadedModel> &m)
                                        { return m && m->backend == "qnn"; });
    removed_models.clear();
    if (had_qnn_backend)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(2000));
    }
    My_Log{} << "[UnloadModelsByDevice] Unloaded " << to_remove.size()
             << " model(s) on device=" << device << std::endl;
}

std::shared_ptr<LoadedModel> ModelManager::GetModel(const std::string &model_name)
{
    std::lock_guard<std::mutex> lock(models_mutex_);
    // 1. 精确匹配（快速路径）
    auto it = loaded_models_.find(model_name);
    if (it != loaded_models_.end())
        return it->second;

    // 2. 大小写不敏感匹配（处理 -c 参数目录名大小写与 service_config.json name 字段不一致的情况）
    // 例如：-c "qwen3-8B-8K/config.json" 加载后 key="qwen3-8B-8K"，
    //       客户端请求 model="Qwen3-8B-8K"，精确匹配失败，此处兜底。
    std::string name_lower = model_name;
    std::transform(name_lower.begin(), name_lower.end(), name_lower.begin(),
                   [](unsigned char c) { return static_cast<unsigned char>(std::tolower(c)); });
    for (const auto &kv: loaded_models_)
    {
        std::string key_lower = kv.first;
        std::transform(key_lower.begin(), key_lower.end(), key_lower.begin(),
                       [](unsigned char c) { return static_cast<unsigned char>(std::tolower(c)); });
        if (key_lower == name_lower)
            return kv.second;
    }
    return nullptr;
}

std::shared_ptr<LoadedModel> ModelManager::GetDefaultModel()
{
    std::lock_guard<std::mutex> lock(models_mutex_);
    if (!default_model_name_.empty())
    {
        auto it = loaded_models_.find(default_model_name_);
        if (it != loaded_models_.end())
        {
            return it->second;
        }
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
    for (const auto &pair: loaded_models_)
    {
        names.push_back(pair.first);
    }
    return names;
}

uint64_t ModelManager::EstimateOtherLoadedModelsMemoryBytes() const
{
    std::lock_guard<std::mutex> lock(models_mutex_);
    uint64_t total = 0;
#ifdef USE_MNN
    for (const auto &pair: loaded_models_)
    {
        const auto &loaded = pair.second;
        if (!loaded || !loaded->config)
        {
            continue;
        }
        // 复用 MNNContext::EstimateMnnMemoryRequirement 对每个其它已加载模型的目录做估算：
        // 若该目录含 .mnn 权重文件（同类 MNN 模型，理论上因"同设备单实例"限制极少并存），
        // 会按真实文件大小计入更准确的估算；否则（QNN/GGUF 等其它后端）至少计入固定安全余量
        // kMnnMemoryEstimateMarginBytes，作为对其驻留内存的保守预留。
        total += MNNContext::EstimateMnnMemoryRequirement(loaded->config->get_model_path());
    }
#endif
    return total;
}

bool ModelManager::LoadAllModelsFromConfig(const std::string &backend_filter)
{
    fs::path service_config_path = fs::path(RootDir) / "service_config.json";
    if (!File::IsFileExist(service_config_path.generic_string()))
    {
        return false;
    }

    // 从 -c 参数所指向的配置文件路径推导 models 目录的绝对路径。
    // 例如：config_file_ = "models\gpt-oss-20b-GGUF\config.json"
    //   → 绝对路径：<CurrentDir>\models\gpt-oss-20b-GGUF\config.json
    //   → 向上两级：<CurrentDir>\models（即 models 目录）
    // 若推导失败（config_file_ 为空或层级不足），则回退到 RootDir。
    fs::path models_base_dir;
    {
        fs::path cfg_path(config_file_);
        // 若 config_file_ 是相对路径，先转换为绝对路径（基于 CurrentDir）
        if (cfg_path.is_relative())
        {
            cfg_path = fs::path(CurrentDir) / cfg_path;
        }
        // 规范化路径（消除 ".." 等符号）
        std::error_code ec;
        fs::path canonical_cfg = fs::weakly_canonical(cfg_path, ec);
        if (!ec)
        {
            cfg_path = canonical_cfg;
        }
        // 向上两级：config.json → model_dir → models_dir
        fs::path model_dir = cfg_path.parent_path();       // 第一级：模型目录（如 gpt-oss-20b-GGUF）
        fs::path candidate_models_dir = model_dir.parent_path(); // 第二级：models 目录
        if (!candidate_models_dir.empty() && candidate_models_dir != model_dir)
        {
            models_base_dir = candidate_models_dir;
            My_Log{My_Log::Level::kInfo} << "[LoadAllModelsFromConfig] Derived models base dir from -c param: "
                                         << models_base_dir.generic_string() << "\n";
        }
        else
        {
            My_Log{My_Log::Level::kWarning}
                    << "[LoadAllModelsFromConfig] Cannot derive models base dir from config_file_='"
                    << config_file_ << "', falling back to RootDir='" << RootDir << "'\n";
            models_base_dir = fs::path(RootDir);
        }
    }

    // 修复：将 models_base_dir 赋值给 model_root_，使 ScanModelDirectory() 和
    // 动态切换逻辑（ChatCompletions）能够正确扫描磁盘上的全部模型目录。
    // model_root_ 原本只在 InitializeConfig（单模型路径）中赋值，
    // 多模型模式下 LoadAllModelsFromConfig 使用局部变量 models_base_dir 但从未写入 model_root_，
    // 导致 ScanModelDirectory() 因 model_root_ 为空而始终返回空列表。
    model_root_ = models_base_dir.generic_string();
    My_Log{My_Log::Level::kInfo} << "[LoadAllModelsFromConfig] model_root_ set to: " << model_root_ << "\n";

    try
    {
        std::ifstream sc_file(service_config_path.generic_string());
        json sc_json;
        sc_file >> sc_json;

        if (!sc_json.contains("models") || !sc_json["models"].is_array())
        {
            return false;
        }

        // 修复：读取 default_model 字段，确保多模型模式下默认模型正确设置
        // 若 service_config.json 中指定了 default_model，则覆盖 LoadSingleModel 设置的默认值
        std::string config_default_model = sc_json.value("default_model", std::string(""));

        int loaded_count = 0;
        for (const auto &m: sc_json["models"])
        {
            std::string name = m.value("name", "");
            std::string path = m.value("path", "");
            std::string backend = m.value("backend", "GGUF");
            std::string device = m.value("device", "gpu");
            int context_size = m.value("context_size", 0);
            bool enabled = m.value("enabled", true);

            if (enabled && !name.empty() && !path.empty())
            {
                if (!backend_filter.empty())
                {
                    std::string backend_lower = backend;
                    std::string filter_lower = backend_filter;
                    std::transform(backend_lower.begin(), backend_lower.end(), backend_lower.begin(), ::tolower);
                    std::transform(filter_lower.begin(), filter_lower.end(), filter_lower.begin(), ::tolower);
                    if (backend_lower != filter_lower)
                    {
                        My_Log{My_Log::Level::kInfo}
                                << "[LoadAllModelsFromConfig] Skipping model '" << name << "' (backend='" << backend
                                << "'): does not match backend_filter='" << backend_filter << "'" << std::endl;
                        continue;
                    }
                }

                My_Log{My_Log::Level::kInfo} << "[LoadAllModelsFromConfig] Processing model: " << name
                                             << ", original path: " << path << "\n";

                // 解析模型目录路径：
                // 若 path 是相对路径，则以从 -c 参数推导出的 models_base_dir 为基准拼接，
                // 而非以 GenieAPIService.exe 的运行目录（RootDir）为基准。
                // 这样无论 exe 放在哪里，只要 -c 参数正确指向模型配置文件，
                // 模型目录路径就能被正确解析。
                fs::path model_path(path);
                if (model_path.is_relative())
                {
                    model_path = models_base_dir / model_path;
                    My_Log{My_Log::Level::kInfo}
                            << "[LoadAllModelsFromConfig] Resolved relative path using models_base_dir: "
                            << model_path.generic_string() << "\n";
                }
                // 修复：规范化路径，消除 ".." 等相对路径符号。
                // 若路径中包含 ".."（如 "GenieService_v2.1.3/../models/xxx"），
                // fs::directory_iterator 和 std::ifstream 在某些平台/实现下可能无法正确处理，
                // 导致 prompt.json 文件存在但被判断为"不存在"，进而 prompt_template 保持 null。
                // 使用 fs::weakly_canonical 规范化（不要求路径实际存在，仅做词法规范化）。
                {
                    std::error_code ec;
                    fs::path canonical_path = fs::weakly_canonical(model_path, ec);
                    if (!ec)
                    {
                        model_path = canonical_path;
                        My_Log{My_Log::Level::kInfo} << "[LoadAllModelsFromConfig] Canonicalized path: "
                                                     << model_path.generic_string() << "\n";
                    }
                    else
                    {
                        My_Log{My_Log::Level::kWarning}
                                << "[LoadAllModelsFromConfig] Failed to canonicalize path, error: " << ec.message()
                                << "\n";
                    }
                }
                path = model_path.generic_string();
                My_Log{My_Log::Level::kInfo} << "[LoadAllModelsFromConfig] Final model path: " << path << "\n";

                // 修复：防止完全重复加载（相同路径 + 相同 backend + 相同 device）。
                // 注意：同一路径但不同 backend/device 是合法的多模型并发场景！
                // 例如：同一 GGUF 模型文件可以同时用 CPU 和 GPU 两个后端加载，
                // 分别对应 qwen-cpu（backend=GGUF, device=cpu）和 qwen-gpu（backend=GGUF, device=gpu）。
                // 因此，只有当路径、backend 和 device 三者完全相同时才跳过（真正的重复加载）。
                //
                // 注意：路径比较时需要规范化已加载模型的路径（可能是相对路径，来自 LoadSingleModel），
                // 与当前的绝对路径（path，已规范化）进行比较，避免因路径格式不同导致比较失败。
                bool skip_due_to_path_conflict = false;
                {
                    std::lock_guard<std::mutex> lock(models_mutex_);
                    for (const auto &pair: loaded_models_)
                    {
                        if (pair.second && pair.second->config)
                        {
                            // 对已加载模型的路径进行规范化，确保与 path（绝对路径）可比较
                            std::string existing_path = pair.second->config->get_model_path();
                            {
                                std::error_code ec2;
                                fs::path ep(existing_path);
                                if (ep.is_relative())
                                    ep = fs::path(CurrentDir) / ep;
                                fs::path canonical_ep = fs::weakly_canonical(ep, ec2);
                                if (!ec2)
                                    existing_path = canonical_ep.generic_string();
                            }

                            if (existing_path == path &&
                                pair.first != name &&
                                pair.second->backend == backend &&
                                pair.second->device == device)
                            {
                                My_Log{My_Log::Level::kWarning}
                                        << "[LoadAllModelsFromConfig] Skipping model '" << name
                                        << "': path='" << path
                                        << "', backend='" << backend
                                        << "', device='" << device
                                        << "' is already loaded as '" << pair.first
                                        << "'. Identical path+backend+device combination." << std::endl;
                                skip_due_to_path_conflict = true;
                                break;
                            }
                        }
                    }
                }
                if (skip_due_to_path_conflict)
                {
                    continue;
                }

                // 修复：防止同一硬件设备（npu/gpu/cpu）上同时驻留 2 个模型。物理 QNN/HTP（或
                // 等价的 GPU/CPU 运行时）会话在硬件层面是独占的——即使每个 LoadedModel 在 C++
                // 层面各自持有独立的 ContextBase 对象，LoadModel() 本身并不会像 ChatCompletions()
                // 的动态切换路径那样先调用 UnloadModelsByDevice()，第二次在同一设备上加载会静默
                // 破坏已驻留模型的底层硬件状态（不报错，但该设备后续请求会命中错乱的输入张量描
                // 述）。自动加载清单应是对 -c 主模型的补充，不应悄悄顶替用户显式指定的主模型，故
                // 这里选择跳过而非卸载重来。
                bool device_conflict = false;
                {
                    std::lock_guard<std::mutex> lock(models_mutex_);
                    for (const auto &pair: loaded_models_)
                    {
                        if (pair.second && pair.second->device == device)
                        {
                            My_Log{My_Log::Level::kWarning}
                                    << "[LoadAllModelsFromConfig] Skipping model '" << name
                                    << "': device '" << device << "' is already occupied by '" << pair.first
                                    << "'. Only one model per device can be resident at a time." << std::endl;
                            device_conflict = true;
                            break;
                        }
                    }
                }
                if (device_conflict)
                {
                    continue;
                }

                // 路径存在性检查：在尝试加载前验证模型目录是否存在，给出清晰的错误日志。
                // 若目录不存在，说明 models_base_dir 推导有误或模型尚未部署，
                // 直接跳过并输出详细错误信息，避免后续加载流程产生难以定位的错误。
                if (!fs::exists(fs::path(path)))
                {
                    My_Log{My_Log::Level::kError}
                            << "[LoadAllModelsFromConfig] Model directory does not exist, skipping model '" << name
                            << "'.\n"
                            << "  Expected path : " << path << "\n"
                            << "  models_base_dir: " << models_base_dir.generic_string() << "\n"
                            << "  Hint: Ensure the -c argument points to the correct model config file "
                            << "(e.g. models\\<model_name>\\config.json), "
                            << "so that the models directory can be correctly derived.\n";
                    continue;
                }

                if (LoadModel(name, backend, device, context_size, path))
                {
                    loaded_count++;
                    My_Log{} << "[LoadAllModelsFromConfig] Loaded model: " << name
                             << " (backend=" << backend << ", device=" << device << ")" << std::endl;
                }
                else
                {
                    My_Log{My_Log::Level::kError} << "[LoadAllModelsFromConfig] Failed to load model: " << name
                                                  << std::endl;
                }
            }
        }

        // 修复：在所有模型加载完成后，应用 default_model 配置
        // 这样可以覆盖 LoadSingleModel 设置的默认值（通常是最后一次 LoadModelByName 加载的模型）
        if (!config_default_model.empty())
        {
            std::lock_guard<std::mutex> lock(models_mutex_);
            if (loaded_models_.find(config_default_model) != loaded_models_.end())
            {
                default_model_name_ = config_default_model;
                My_Log{} << "[LoadAllModelsFromConfig] Default model set to: " << default_model_name_ << std::endl;
            }
            else
            {
                My_Log{My_Log::Level::kWarning} << "[LoadAllModelsFromConfig] Specified default_model '"
                                                << config_default_model
                                                << "' not found in loaded models, keeping current default: "
                                                << default_model_name_ << std::endl;
            }
        }

        My_Log{} << "[LoadAllModelsFromConfig] Total models loaded: " << loaded_count
                 << ", default_model=" << default_model_name_ << std::endl;
        return loaded_count > 0;
    }
    catch (const std::exception &e)
    {
        My_Log{My_Log::Level::kError} << "Failed to load models from config: " << e.what() << std::endl;
        return false;
    }
}

bool ModelManager::LoadModel(const std::string &model_name,
                             const std::string &backend,
                             const std::string &device,
                             int context_size_in,
                             const std::string &model_path_in)
{
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        if (loaded_models_.find(model_name) != loaded_models_.end())
        {
            My_Log{} << "Model " << model_name << " already loaded.\n";
            return true;
        }
    }

    // 重置最近一次加载失败原因，避免上一次不相关的失败（如另一个模型的内存不足）
    // 被误读为本次加载失败的原因。
    SetLastLoadFailureReason(LoadFailureReason::kNone, "");

    Clean();
    auto config = std::make_shared<ModelInstanceConfig>();
    config->set_model_name(model_name);
    config->set_model_path(model_path_in.empty() ? model_root_ + "/" + model_name : model_path_in);
    config_file_ = config->get_model_path() + "/config.json";
    config->set_backend(backend);
    config->set_device(device);

#ifdef GENIEAPI_EXPORTS
    {
        std::string backend_lower = backend;
        std::transform(backend_lower.begin(), backend_lower.end(), backend_lower.begin(),
                       [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        if (backend_lower != "qnn")
        {
            My_Log{My_Log::Level::kWarning} << "[LoadModel] library mode only allows qnn backend, skip model '"
                                             << model_name << "' (backend=" << backend << ")" << std::endl;
            return false;
        }
    }
#endif

    // Resolve prompt path and load prompt.json
    // Logic adapted from LoadPromptTemplates but using local variables
    std::string prompt_path = config->get_model_path() + "/prompt.json";
    std::string known_path;
    // config.json 回退只允许精确匹配：只有下面的精确匹配分支才会写入 known_exact_path，
    // 前缀匹配命中时保持为空，避免 ModeVerifierImpl::CreateIfVerified() 借用仅前缀相同的
    // 其它模型的 config.json（与 LoadPromptTemplates() 对 known_model_path_ 的既有语义一致）。
    std::string known_exact_path;

    if (!File::IsFileExist(prompt_path) || File::IsFileEmpty(prompt_path))
    {
        My_Log{My_Log::Level::kWarning} << "[LoadModel] Prompt file not found at primary path, trying known paths...\n";

        // Try known paths logic
        // We use ResolveKnownModelPath but need to be careful not to rely on member state
        // ModelManager::ResolveKnownModelPath implementation uses static config_list_ready and iterates config_model_name_list_
        // It takes model_feature as arg.
        // We can pass model_name to it.
        known_path = ResolveKnownModelPath(model_name, false);

        if (!known_path.empty())
        {
            prompt_path = known_path + "/prompt.json";
            known_exact_path = known_path;
            My_Log{My_Log::Level::kInfo} << "[LoadModel] Trying known_path: " << prompt_path << "\n";
        }
        else
        {
            static std::array<std::string, 9> models_prefix{
                    "allam-7b-ssd", "deepseek-r1-distill-qwen-7B", "hunyuan2B", "ibm-granite-v3.1-8b",
                    "llama2.0-7b", "llama3.1-8b", "phi", "qwen", "gpt-oss-20b",
            };
            for (const auto &model_prefix: models_prefix)
            {
                if (ModelComparer(model_name, model_prefix, true))
                {
                    known_path = ResolveKnownModelPath(model_prefix, true);
                    prompt_path = known_path + "/prompt.json";
                    My_Log{My_Log::Level::kInfo} << "[LoadModel] Trying model_prefix '" << model_prefix << "': "
                                                 << prompt_path << "\n";
                    break;
                }
            }
        }
    }

    known_model_path_ = known_exact_path;

    // 修复：使用 json::object() 而非 json{}（后者在 ordered_json 中是 array 类型）
    PromptType pt = {PromptType::Unknown};
    json prompt_json = json::object();
    int context_size = (context_size_in > 0) ? context_size_in : 4096; // Default or override

    // 从 config.json 读取 dialog.context.size（QNN/SSD 格式）
    // 优先级：service_config.json > config.json > prompt.json > 默认值
    int config_json_ctx_size = ParseContextSizeFromConfigJson(config_file_, model_name, "[LoadModel]");

    My_Log{My_Log::Level::kInfo} << "[LoadModel] Final prompt_path before ParsePromptFile: " << prompt_path << "\n";

    {
        int json_ctx_size = 0;
        ParsePromptFile(prompt_path, model_name, prompt_json, pt, json_ctx_size);

        My_Log{My_Log::Level::kInfo}
                << "[LoadModel] ParsePromptFile returned: pt=" << pt.to_string()
                << ", prompt_json.type='" << prompt_json.type_name()
                << "', prompt_json.is_object=" << prompt_json.is_object()
                << ", json_ctx_size=" << json_ctx_size
                << " for model '" << model_name << "'\n";

        // context_size 优先级：service_config.json > config.json > prompt.json > 默认值
        if (context_size_in > 0)
        {
            // service_config.json 显式指定，优先级最高，保持不变
            if (config_json_ctx_size > 0 && context_size_in != config_json_ctx_size)
            {
                My_Log{My_Log::Level::kWarning}
                        << "[LoadModel] Model " << model_name << ": context_size in service_config ("
                        << context_size_in << ") overrides config.json (" << config_json_ctx_size << ")\n";
            }
            else if (json_ctx_size > 0 && context_size_in != json_ctx_size)
            {
                My_Log{My_Log::Level::kWarning}
                        << "[LoadModel] Model " << model_name << ": context_size in service_config ("
                        << context_size_in << ") overrides prompt.json (" << json_ctx_size << ")\n";
            }
        }
        else if (config_json_ctx_size > 0)
        {
            // config.json 中有 dialog.context.size，优先级次之
            context_size = config_json_ctx_size;
        }
        else if (json_ctx_size > 0)
        {
            // prompt.json 中有 context_size，优先级再次之
            context_size = json_ctx_size;
        }
        // 否则保持默认值 4096
    }

    // 修复：若 ParsePromptFile 失败（pt==Unknown），prompt_json 可能不是有效 object。
    // 强制确保 prompt_json 始终是 object 类型，防止 ChatHistory::GetUserMessage 收到 array 类型。
    if (!prompt_json.is_object())
    {
        My_Log{My_Log::Level::kError}
                << "[LoadModel] prompt_json is NOT an object (type='" << prompt_json.type_name()
                << "') after ParsePromptFile for model '" << model_name
                << "'. Resetting to empty object to prevent downstream type=array crash.\n";
        prompt_json = json::object();
    }

    config->set_prompt_template(prompt_json);
    config->set_prompt_type(pt);
    config->set_context_size(context_size);

    // Detect thinking model
    bool is_thinking = str_contains(model_name, "Qwen3") ||
                       str_contains(model_name, "DeepSeek") ||
                       str_contains(model_name, "Hunyuan");
    config->set_thinking_model(is_thinking);

    // Copy global settings (could be overridden per model in future)
    config->set_lora_adapter(loraAdapter);
    config->set_lora_alpha(loraAlpha);
    config->set_output_all_text(outputAllText);
    config->set_enable_thinking(enableThinking);
    config->set_enable_prompt_debug(enablePromptDebug);
    config->set_num_response(num_response_);
    config->set_min_output_num(minOutputNum);

    auto context = ModeVerifier::TryCreate(config.get(), this);
    if (!context)
    {
        My_Log{My_Log::Level::kError} << RED << "Load Model Failed: " << model_name << RESET << std::endl;
        return false;
    }

    // [Refactor] Infer backend/device from detected model format if they are defaults/mismatch
    {
        ModelFormat fmt = config->get_model_format();
        if (fmt == ModelFormat::QNN)
        {
            // QNN 模型只能跑在 NPU 上，强制覆盖
            config->set_backend("qnn");
            config->set_device("npu");
        }
        else if (fmt == ModelFormat::MNN)
        {
            // MNN 模型只能跑在 CPU 上，强制覆盖
            config->set_backend("mnn");
            config->set_device("cpu");
        }
        else if (fmt == ModelFormat::GGUF)
        {
            // GGUF 模型通过 llama.cpp 后端运行，优先 GPU，可 fallback CPU。
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

            My_Log{} << "[LoadModel] GGUF model: backend=GGUF, device=" << config->get_device() << std::endl;
        }
    }

    auto loaded_model = std::make_shared<LoadedModel>();
    loaded_model->config = config;
    loaded_model->context = context;
    loaded_model->backend = config->get_backend();
    loaded_model->device = config->get_device();
    loaded_model->is_loaded = true;

    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        loaded_models_[model_name] = loaded_model;
        if (default_model_name_.empty())
        {
            default_model_name_ = model_name;
        }
    }

    My_Log{} << GREEN << "Model loaded successfully: " << model_name << RESET << std::endl;
    return true;
}

// ============================================================
// ParseContextSizeFromConfigJson: 从 config.json 中读取 dialog.context.size。
// 两处调用点（LoadModel 多模型路径 和 LoadPromptTemplates 单模型路径）共用此函数，
// 避免重复代码。
//
// 参数：
//   config_json_path - config.json 的完整路径
//   model_name        - 模型名称（仅用于日志）
//   log_prefix         - 日志前缀（如 "[LoadModel]" / "[LoadPromptTemplates]"）
//
// 返回值：
//   config.json 中 dialog.context.size（0 表示未指定或解析失败）
// ============================================================
static int ParseContextSizeFromConfigJson(const std::string &config_json_path,
                                          const std::string &model_name,
                                          const std::string &log_prefix)
{
    int config_json_ctx_size = 0;
    if (File::IsFileExist(config_json_path) && !File::IsFileEmpty(config_json_path))
    {
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

// ============================================================
// ParsePromptFile: 从指定路径解析 prompt.json，提取 prompt 模板、类型和 context_size。
// 两处调用点（LoadModel 多模型路径 和 LoadPromptTemplates 单模型路径）共用此函数，
// 避免重复代码，并统一修复 json{} 语法可能产生 array 的问题。
//
// 参数：
//   prompt_path  - prompt.json 的完整路径（已规范化，不含 ".."）
//   model_name   - 模型名称（仅用于日志）
//
// 返回值（通过输出参数）：
//   out_prompt   - 解析后的 prompt 模板（json::object），失败时为 null
//   out_pt       - 检测到的 PromptType（General/Harmony），失败时为 Unknown
//   out_ctx_size - prompt.json 中的 context_size（0 表示未指定或解析失败）
// ============================================================
static void ParsePromptFile(const std::string &prompt_path,
                            const std::string &model_name,
                            json &out_prompt,
                            PromptType &out_pt,
                            int &out_ctx_size)
{
    // 修复：使用 json::object() 而不是 json{}，避免被解析为 array 类型
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
            // 修复：保持 out_prompt 为 json::object()（已在函数开头初始化），不赋值 json{}
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
            // 修复：保持 out_prompt 为 json::object()，不赋值 json{}
            return;
        }

        // 使用逐字段赋值方式构建 out_prompt，确保类型为 object。
        // 注意：json{{"key","val"},...} 语法在 nlohmann::ordered_json 中可能被解析为 array，
        // 导致后续 ChatHistory::GetUserMessage 中 type_name()="array" 的错误。
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

        // 打印各字段长度，避免 substr 在空字符串上崩溃
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

        // 解析 context_size
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

        // 检测 prompt 类型
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
        // 修复：使用 json::object() 而非 json{}。
        // 在 nlohmann::ordered_json 中，json{} 默认构造为 array 类型（空数组），
        // 而非 null 或 object，会导致 ChatHistory::GetUserMessage 中出现 type=array 错误。
        out_prompt = json::object();
        out_pt = PromptType::Unknown;
        out_ctx_size = 0;
    }
}

/* When use prompt json?
 * Determine model response processor
 * build prompt from chathistroy(TextQuery), add History
 * */
PromptType ModelManager::LoadPromptTemplates(std::string &&prompt_path)
{
    PromptType pt{PromptType::Unknown};

    if (!File::IsFileExist(prompt_path) || File::IsFileEmpty(prompt_path))
    {
        std::string org_prompt_path = prompt_path;
        known_model_path_ = ResolveKnownModelPath(model_name_, false);
        if (!known_model_path_.empty())
        {
            prompt_path = known_model_path_ + "/prompt.json";
            My_Log{} << "get known model path successfully\n";
            goto ahead;
        }

        // Unique prompt identifier for a set of models
        // 修复：数组大小从10改为9，与实际元素数量一致，避免末尾空字符串导致 ModelComparer 意外匹配
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
            // check if model match one of the prefix
            if (ModelComparer(model_name_, model_prefix, true))
            {
                // one of them, such as qwen match qwen2.0-7b or qwen2.0-7b-ssd or qwen3
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

    // 调用公共辅助函数解析 prompt.json
    int json_ctx_size = 0;
    ParsePromptFile(prompt_path, model_name_, prompt_, pt, json_ctx_size);

    if (pt == PromptType::Unknown)
    {
        // ParsePromptFile 失败（文件不存在、格式错误等），prompt_ 保持 null
        return pt;
    }

    // 从 config.json 读取 dialog.context.size（QNN/SSD 格式）
    // 优先级：service_config.json（context_size_ 已由外部设置）> config.json > prompt.json > 默认值
    // 优先使用 known_model_path_（已解析的模型路径），否则使用 model_path_
    std::string base_path = !known_model_path_.empty() ? known_model_path_ : model_path_;
    int config_json_ctx_size = ParseContextSizeFromConfigJson(base_path + "/config.json", model_name_, "[LoadPromptTemplates]");

    // 更新 context_size_（优先级：已由外部设置的值 > config.json > prompt.json > 默认值）
    // 注意：context_size_ 初始值为 DEFAULT_CONTEXT_SIZE（4096），若外部未显式设置则仍为默认值
    if (context_size_ != DEFAULT_CONTEXT_SIZE)
    {
        // 外部（service_config.json）已显式设置，保持不变，仅打印警告
        if (config_json_ctx_size > 0 && context_size_ != config_json_ctx_size)
        {
            My_Log{My_Log::Level::kWarning}
                    << "[LoadPromptTemplates] Model " << model_name_ << ": context_size already set ("
                    << context_size_ << ") overrides config.json (" << config_json_ctx_size << ")\n";
        }
        else if (json_ctx_size > 0 && context_size_ != json_ctx_size)
        {
            My_Log{My_Log::Level::kWarning}
                    << "[LoadPromptTemplates] Model " << model_name_ << ": context_size already set ("
                    << context_size_ << ") overrides prompt.json (" << json_ctx_size << ")\n";
        }
        My_Log{My_Log::Level::kInfo} << "contextSize (from external config): " << context_size_ << std::endl;
    }
    else if (config_json_ctx_size > 0)
    {
        context_size_ = config_json_ctx_size;
        My_Log{My_Log::Level::kInfo} << "contextSize (from config.json): " << context_size_ << std::endl;
    }
    else if (json_ctx_size > 0)
    {
        context_size_ = json_ctx_size;
        My_Log{My_Log::Level::kInfo} << "contextSize (from prompt.json): " << context_size_ << std::endl;
    }
    else
    {
        My_Log{My_Log::Level::kWarning}
                << "context_size field missing or invalid in both config.json and prompt.json, using default: "
                << context_size_ << std::endl;
    }

    return pt;
}

json IModelConfig::get_model_list() const
{
    UpdateModeList();
    json jsonData;
    std::vector<json> models;
    for (auto &mode_name: model_list_)
    {
        json model;
        model["id"] = mode_name;
        model["object"] = "model";
        model["created"] = timer.GetSystemTime();
        model["owned_by"] = "owner";
        model["permission"] = json::array();
        models.push_back(model);
    }
    jsonData["data"] = models;
    jsonData["object"] = "list";
    return jsonData;
}

void IModelConfig::UpdateModeList() const
{
    model_list_.clear();
    for (const auto &entry: fs::directory_iterator(model_root_))
    {
        if (!entry.is_directory())
        {
            continue;
        }
        model_list_.push_back(entry.path().filename().generic_string());
    }
}

void ModelManager::Clean()
{
    // 记录本次清理前是否持有 QNN/NPU 句柄：genieModelHandle 置空触发 shared_ptr 归零、
    // 同步执行 ~GenieContext()（NPU/HTP 驱动异步释放资源，需要显式等待才安全）。
    // 这是全项目里唯一真正触发该释放的位置，UnloadModel()/UnloadModelsByDevice() 均经过此处，
    // 因此把等待下沉到这里可以让两条路径同时获得同等保护，且不拖慢 MNN/GGUF（不持有该句柄）的卸载耗时。
    bool had_qnn_handle = (genieModelHandle != nullptr);
    known_model_path_.clear();
    genieModelHandle = nullptr;
    qnn_embedding_.Clean();
    if (had_qnn_handle)
    {
        // Extra delay to ensure NPU resources are fully released before the next load/restart.
        std::this_thread::sleep_for(std::chrono::milliseconds(2000));
    }
}

std::string ModelManager::ResolveKnownModelPath(const std::string &model_feature, bool only_prefix)
{
    static bool config_list_ready{
            [this]()
            {
                fs::path config_path{RootDir + "/config"};
                if (!fs::is_directory(config_path))
                {
                    return true;
                }
                for (const auto &entry: fs::directory_iterator(RootDir + "/config"))
                {
                    if (!entry.is_directory())
                    {
                        continue;
                    }
                    config_model_name_list_.push_back(entry.path().filename().generic_string());
                }
                return true;
            }()
    };

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
