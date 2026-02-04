//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "log.h"
#include "utils.h"
#include "model_manager.h"
#include "../context/qnn/genie.h"
#include "../context/mnn.h"
#include "../context/llama_cpp.h"
#include "def.h"
#include <filesystem>

namespace fs = std::filesystem;

#include <LibAppBuilder.hpp>

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
                    My_Log{My_Log::Level::kError} << "veg file: " << files.serialized_file_ << " is invalid\n";
                    continue;
                }

                for (auto &bin_file: files.bin_files_stack_)
                {
                    if (!File::IsFileExist(bin_file) || File::IsFileEmpty(bin_file))
                    {
                        My_Log{My_Log::Level::kError} << "bin file: " << bin_file << " is invalid\n";
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
                    infer_resource->bin_stacks_.emplace_back(File::ReadFile(bin_file));
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
                    {
                            model_path + "/raw/position_ids.bin",
                            model_path + "/raw/attention_mask.bin"
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
                                               {
                                                       model_path + "/qwen2.5_omini_audio/padded_feature.raw",
                                                       model_path + "/qwen2.5_omini_audio/padded_mask.raw",
                                                       model_path + "/qwen2.5_omini_audio/attention_mask.raw"
                                               }
                                       });

            embedding_file_set.emplace(ModelType{ModelType::Vision},
                                       EmbeddingFileSet{
                                               model_path + "/veg.serialized.bin", {
                                                       model_path + "/qwen2.5_omini_vision/position_ids_cos.raw",
                                                       model_path + "/qwen2.5_omini_vision/position_ids_sin.raw",
                                                       model_path + "/qwen2.5_omini_vision/window_attention_mask.raw",
                                                       model_path + "qwen2.5_omini_vision/full_attention_mask.raw"
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
        };

        QNNEmbedding embedding{};
        for (const auto &check: checkers)
        {
            My_Log{} << "try to check if qnn embedding is: "
                     << check.embedding_type_.to_string() << "\n";

            embedding = check.func_();
            if (embedding.embedding_type_ != QNNEmbeddingType::None)
            {
                embedding.embedded_raw_buf_ = File::ReadFile(raw_path);
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
        explicit ModeVerifierImpl(ModelManager *self, bool config_strict = true) : self_{self}
        {
            if (File::IsFileExist(self_->config_file_) && !File::IsFileEmpty(self_->config_file_))
                return;

            if (self->known_model_path_.empty())
            {
                std::string err_str{"config file is not found: " + self_->config_file_};
                config_strict ? throw std::runtime_error(err_str)
                              : My_Log{My_Log::Level::kWarning} << err_str << std::endl;
                return;
            }

            std::string new_config_path{self->known_model_path_ + "/config.json"};
            My_Log{My_Log::Level::kError} << "config file: " << self_->config_file_ << " "
                                          << "is not exist, will use default ver: " << new_config_path
                                          << std::endl;
            self_->config_file_ = new_config_path;
        }

        virtual ~ModeVerifierImpl() = default;

    protected:
        ModelManager *self_;

    private:
        virtual std::shared_ptr<ContextBase> CreateIfVerified() = 0;
    };

    struct QnnVerifier : public ModeVerifierImpl
    {
        explicit QnnVerifier(ModelManager *self) : ModeVerifierImpl(self) {}

        std::shared_ptr<ContextBase> CreateIfVerified() override
        {
            if (!File::MatchFileInDir(self_->model_path_, ".bin"))
            {
                return nullptr;
            }

            {
                std::ifstream file(self_->config_file_);
                json j;
                file >> j;

                if (!j.contains("dialog"))
                {
                    return nullptr;
                }
            }

            std::vector<std::string> files;
            // TODO: use Regex embedding_weights_xxx.raw
            if (!File::MatchFileInDir(self_->model_path_, "embedding_weights", &files))
            {
                goto ahead;
            }

            if (File::IsFileEmpty(files[0]))
            {
                throw std::runtime_error("embedded file: " + files[0] + " is invalid");
            }

            self_->qnn_embedding_ = QNNImpl::TryCreate(self_->model_path_, files[0]);
            if (self_->qnn_embedding_.embedding_type_ == QNNEmbeddingType::None)
                throw std::runtime_error("qnn model does not match any embedding rules");

            My_Log{} << "check qnn embedding model type: " << self_->qnn_embedding_.model_types_.to_string() << "\n";
            ahead:
            return std::make_shared<GenieContext>(*self_);
        }
    };

    struct MnnVerifier : public ModeVerifierImpl
    {
        explicit MnnVerifier(ModelManager *self) : ModeVerifierImpl(self) {}

        std::shared_ptr<ContextBase> CreateIfVerified() override
        {
            if (!File::MatchFileInDir(self_->model_path_, ".mnn"))
            {
                return nullptr;
            }
            return std::make_shared<MNNContext>(*self_);
        }
    };

    struct GGUFVerify : public ModeVerifierImpl
    {
        explicit GGUFVerify(ModelManager *self) : ModeVerifierImpl(self, true) {}

        std::shared_ptr<ContextBase> CreateIfVerified() override
        {
            if (!File::MatchFileInDir(self_->model_path_, ".gguf"))
            {
                return nullptr;
            }

            return std::make_shared<LLAMACppBuilder>(*self_);
        }
    };

    static std::shared_ptr<ContextBase> TryCreate(ModelManager *self)
    {
        struct Checker
        {
            ModelFormat model_format_;
            std::function<std::shared_ptr<ContextBase>()> func_;
        };

        /* @formatter:off */
        std::vector<Checker> checkers{
                {{ModelFormat::QNN}, [self](){ return QnnVerifier(self).CreateIfVerified();}},
#ifdef USE_MNN
                {{ModelFormat::MNN}, [self](){ return MnnVerifier(self).CreateIfVerified();}},
#endif
#ifdef USE_GGUF
                {{ModelFormat::GGUF},[self](){ return GGUFVerify(self).CreateIfVerified();}}
#endif
        };
        /* @formatter:on */

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
                    self->model_format_ = check.model_format_;
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

ModelManager::ModelManager(IModelConfig &&config) :
        IModelConfig{std::move(config)} {}

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

    return LoadModel();
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

    if (!load)
        return true;

    return LoadModel();
}

bool ModelManager::LoadModel()
{
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

    genieModelHandle = ModeVerifier::TryCreate(this);
    if (!genieModelHandle)
    {
        My_Log{} << RED << "Load Model Failed, Model Name: " << model_name_ << RESET << std::endl;
        model_name_.clear();
        return false;
    }

    My_Log{} << GREEN << "Model load successfully: " << model_name_ << RESET << std::endl;
    loaded_ = true;
    return true;
}

void ModelManager::UnloadModel()
{
    if (genieModelHandle == nullptr)
    {
        My_Log{My_Log::Level::kError} << "unload model without init" << std::endl;
    }
    else
    {
        Clean();
        model_name_.clear();
    }
}

/* When use prompt json?
 * Determine model response processor
 * build prompt from chathistroy(TextQuery), add History
 * */
PromptType ModelManager::LoadPromptTemplates(std::string &&prompt_path)
{
    PromptType pt{PromptType::Unknown};
    json j;

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
        static std::array<std::string, 10> models_prefix{
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

    std::ifstream file(prompt_path);
    if (!file.good())
    {
        My_Log{My_Log::Level::kError} << "cannot open prompt file: " << prompt_path << std::endl;
        pt = PromptType::Unknown;
        return pt;
    }

    try
    {
        file >> j;
        prompt_ = json{
                {"system",    j["prompt_system"].get<std::string>()},
                {"user",      j["prompt_user"].get<std::string>()},
                {"assistant", j["prompt_assistant"].get<std::string>()},
                {"tool",      j["prompt_tool"].get<std::string>()},
                {"start",     j["prompt_start"].get<std::string>()}
        };
    }
    catch (const std::exception &/*e*/)
    {
        My_Log{} << "prompt file: " << prompt_path << " is invalid" << std::endl;
        return pt;
    }

    try
    {
        context_size_ = j["context_size"];
    }
    catch (const std::exception &/*e*/)
    {
        context_size_ = DEFAULT_CONTEXT_SIZE;
    }

    pt = str_contains(prompt_["assistant"], "<|channel|>");
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
    known_model_path_.clear();
    genieModelHandle = nullptr;
    qnn_embedding_.Clean();
}

std::string ModelManager::ResolveKnownModelPath(const std::string &model_feature, bool only_prefix)
{
    static bool config_list_ready{
            [this]()
            {
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
                SetLogLevel(GENIE_LOG_LEVEL_ERROR, "");
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