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
#include "../context/genie.h"
#include "../context/mnn.h"
#include "../context/llama_cpp.h"

#include <filesystem>

namespace fs = std::filesystem;

class ModelManager::EmbeddingVerifier
{
    struct EmbeddingVerifierImpl
    {
        explicit EmbeddingVerifierImpl(ModelManager *self) : self_{self}
        {}

        QNNEmbeddingType type_{};
        std::string init_bin_file_;
        std::vector<std::string> bin_files_stack_{};
        ModelManager *self_{};

        QNNEmbeddingInfo CreateIfVerified()
        {
            if (!File::IsFileExist(init_bin_file_) || File::IsFileEmpty(init_bin_file_))
            {
                My_Log{My_Log::Level::kError} << "init bin file: " << init_bin_file_ << " is invalid\n";
                return {};
            }

            for (auto &bin_file: bin_files_stack_)
            {
                if (!File::IsFileExist(bin_file) || File::IsFileEmpty(bin_file))
                {
                    My_Log{My_Log::Level::kError} << "bin file: " << bin_file << " is invalid\n";
                    return {};
                }
            }

            QNNEmbeddingInfo embedding_info;
            embedding_info.type_.v_ = type_.v_;
            embedding_info.init_bin_file_ = init_bin_file_;
            embedding_info.bin_stack_.resize(bin_files_stack_.size());

            for (auto i = 0; i < bin_files_stack_.size(); ++i)
            {
                embedding_info.bin_stack_[i] = std::move(File::ReadFile(bin_files_stack_[i]));
            }
            return embedding_info;
        };
    };

    struct PHI4Verifier : public EmbeddingVerifierImpl
    {
        explicit PHI4Verifier(ModelManager *self) : EmbeddingVerifierImpl(self)
        {
            type_.v_ = QNNEmbeddingType::PHI4MM;
            init_bin_file_ = self_->model_path_ + "/veg.serialized.bin";
            bin_files_stack_.assign({self_->model_path_ + "/position_ids.bin",
                                     self_->model_path_ + "/attention_mask.bin"});
        }
    };

    struct Qwen2_5Verifier : public EmbeddingVerifierImpl
    {
        explicit Qwen2_5Verifier(ModelManager *self) : EmbeddingVerifierImpl(self)
        {
            type_.v_ = QNNEmbeddingType::QWEN2_5;
            init_bin_file_ = self_->model_path_ + "/veg.serialized.bin";
            bin_files_stack_.assign({self_->model_path_ + "/position_ids_cos.raw",
                                     self_->model_path_ + "/position_ids_sin.raw",
                                     self_->model_path_ + "/window_attention_mask.raw",
                                     self_->model_path_ + "/full_attention_mask.raw"});
        }
    };

public:
    static QNNEmbeddingInfo TryCreate(ModelManager *self)
    {
        struct Checker
        {
            QNNEmbeddingType embedding_type_;
            std::function<QNNEmbeddingInfo()> func_;
        };

        /* @formatter:off */
        std::vector<Checker> checkers{
                {{QNNEmbeddingType::PHI4MM}, [self](){ return PHI4Verifier(self).CreateIfVerified();}},
                {{QNNEmbeddingType::QWEN2_5}, [self](){ return Qwen2_5Verifier(self).CreateIfVerified();}},
        };
        /* @formatter:on */

        QNNEmbeddingInfo embedding_info;
        for (const auto &check: checkers)
        {
            My_Log{} << "try to check if qnn embedding is: "
                     << check.embedding_type_.to_string() << "\n";

            embedding_info = check.func_();
            if (embedding_info.type_.v_ != QNNEmbeddingType::Unknown)
            {
                return embedding_info;
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
                config_strict ? throw std::runtime_error(err_str) :
                My_Log{My_Log::Level::kWarning} << err_str << std::endl;
                return;
            }

            std::string new_config_path{self->known_model_path_ + "/config.json"};
            My_Log{My_Log::Level::kError} << "config file: " << self_->config_file_ << " "
                                          << "is not exist, will use default ver: " << new_config_path
                                          << std::endl;
            self_->config_file_ = new_config_path;
        }

    protected:
        ModelManager *self_;

    private:
        virtual std::shared_ptr<ContextBase> CreateIfVerified() = 0;
    };

    struct QnnVerifier : public ModeVerifierImpl
    {
        explicit QnnVerifier(ModelManager *self) : ModeVerifierImpl(self)
        {}

        std::shared_ptr<ContextBase> CreateIfVerified() override
        {
            if (!File::MatchFileInDir(self_->model_path_, ".bin", true))
            {
                return nullptr;
            }

            std::ifstream file(self_->config_file_);
            json j;
            file >> j;

            if (!j.contains("dialog"))
            {
                return nullptr;
            }

            if (self_->query_type_.v_ != QueryType::EmbeddingQuery)
                goto ahead;

            self_->qnn_embedding_info_ = EmbeddingVerifier::TryCreate(self_);
            if (self_->qnn_embedding_info_.type_.v_ == QNNEmbeddingType::Unknown)
            {
                throw std::runtime_error("qnn model does not match any embedding");
            }

            ahead:
            return std::make_shared<GenieContext>(*self_);
        }
    };

    struct MnnVerifier : public ModeVerifierImpl
    {
        explicit MnnVerifier(ModelManager *self) : ModeVerifierImpl(self)
        {}

        std::shared_ptr<ContextBase> CreateIfVerified() override
        {
            if (!File::MatchFileInDir(self_->model_path_, ".mnn", true))
            {
                return nullptr;
            }
            return std::make_shared<MNNContext>(*self_);
        }
    };

    struct GGUFVerify : public ModeVerifierImpl
    {
        explicit GGUFVerify(ModelManager *self) : ModeVerifierImpl(self, true)
        {}

        std::shared_ptr<ContextBase> CreateIfVerified() override
        {
            if (!File::MatchFileInDir(self_->model_path_, ".gguf", true))
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
            ModelType model_type_;
            std::function<std::shared_ptr<ContextBase>()> func_;
        };

        /* @formatter:off */
        std::vector<Checker> checkers{
                {{ModelType::QNN}, [self](){ return QnnVerifier(self).CreateIfVerified();}},
#ifdef USE_MNN
                {{ModelType::MNN}, [self](){ return MnnVerifier(self).CreateIfVerified();}},
#endif
#ifdef USE_GGUF
                {{ModelType::GGUF},[self](){ return GGUFVerify(self).CreateIfVerified();}}
#endif
        };
        /* @formatter:on */

        std::shared_ptr<ContextBase> context;

        try
        {
            for (const auto &check: checkers)
            {
                My_Log{} << "try to check if model is: "
                         << const_cast<ModelType &>(check.model_type_).to_string()
                         << " model\n";

                auto use_second = MeasureSeconds(
                        [&context, &check]()
                        { context = check.func_(); }
                );

                if (context)
                {
                    My_Log{} << "load successfully! use second: " << use_second << " \n";
                    self->model_type_ = check.model_type_;
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
        IModelConfig{std::move(config)}
{}

bool ModelManager::LoadModelByName(const std::string &model_name, bool &first_load)
{
    loaded_ = false;
    if (model_name.empty())
    {
        My_Log{My_Log::Level::kError} << "model name can not be empty" << std::endl;
        return false;
    }

    if (model_name_ == model_name && genieModelHandle)
    {
        My_Log{} << "model: " + model_name << " has already been loaded" << std::endl;
        first_load = false;
        loaded_ = true;
        return true;
    }
    first_load = true;

    My_Log{} << "model name: " + model_name << " will be loaded" << std::endl;
    UpdateModeList();
    bool found{false};
    for (const auto &name: model_list_)
    {
        if (ModelComparer(name, model_name, false))
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
        My_Log{My_Log::Level::kError} << "model name: " << model_name_ << " is not exist" << std::endl;
        return false;
    }

    return LoadModel();
}

bool ModelManager::InitializeConfig(bool load)
{
    fs::path config_path{config_file_};
    My_Log{} << "ModelManager::LoadModel,configFile=" + config_path.generic_string() << std::endl;

    auto check_path{[](const fs::path &path)
                    {
                        auto str = path.generic_string();
                        if (str.empty())
                            throw std::runtime_error("the model file layout does not meet the standard, "
                                                     "it must be /models/{model_name}/{config}");
                        return str;
                    }};

    try
    {
        model_path_ = check_path(config_path.parent_path().generic_string());
        model_name_ = config_path.parent_path().filename().generic_string();
        model_root_ = check_path(config_path.parent_path().parent_path().generic_string());
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
    query_type_ = CheckModelQueryStyle();
    if (query_type_.v_ == QueryType::Unknown)
        return false;
    My_Log{} << "check the query type: " << query_type_.to_string() << std::endl;

    prompt_type_ = LoadPromptTemplates(model_path_ + "/prompt.json");
    if (prompt_type_.v_ == PromptType::Unknown)
        return false;
    My_Log{} << "check the prompt type: " << prompt_type_.to_string() << "\n";

    thinking_model_ = [this]() -> bool
    {
        return str_contains(model_name_, "Qwen3") ||
               str_contains(model_name_, "DeepSeek") ||
               str_contains(model_name_, "Hunyuan");
    }();
    My_Log{} << "check is think model: " << thinking_model_ << "\n";

    genieModelHandle = ModeVerifier::TryCreate(this);
    if (!genieModelHandle)
    {
        My_Log{} << RED << "Load Model Failed, Model Name: " << model_name_ << RESET << std::endl;
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
        static std::array<std::string, 9> models_prefix{
                "Allam-7B-SSD",
                "DeepSeek-R1-Distill-Qwen-7B",
                "Hunyuan2B",
                "IBM-Granite-v3.1-8B",
                "Llama2.0-7B-SSD",
                "Llama3.1-8B-SSD",
                "phi",
                "qwen2.0",
                "gpt-oss-20b",
        };

        prompt_path.clear();
        for (const auto &model_prefix: models_prefix)
        {
            // check if model match one of the prefix
            if (ModelComparer(model_name_, model_prefix, true))
            {
                // one of them, such as qwen2.0 match qwen2.0-7b or qwen2.0-7b-ssd
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
        pt.v_ = PromptType::Unknown;
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
    catch (const std::exception &e)
    {
        My_Log{} << "prompt file: " << prompt_path << " is invalid" << std::endl;
        return pt;
    }

    try
    {
        contextSize = j["context_size"];
    }
    catch (const std::exception &e)
    {
        contextSize = CONTEXT_SIZE;
    }

    pt.v_ = str_contains(prompt_["assistant"], "<|channel|>");
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

QueryType ModelManager::CheckModelQueryStyle()
{
    QueryType qt{QueryType::Unknown};
    std::vector<std::string> files = File::SearchExtInDir(model_path_, ".raw");
    if (files.empty())
    {
        qt.v_ = QueryType::TextQuery;
        return qt;
    }

    int index{-1};
    for (auto i = 0; i < files.size(); ++i)
    {
        if (str_contains(files[i], "embedding_weights"))
        {
            index = i;
            break;
        }
    }

    if (index == -1)
    {
        My_Log{My_Log::Level::kError} << "embedded file is not exist\n";
        return qt;
    }

    const auto &embedded_file = files[index];
    if (File::IsFileEmpty(embedded_file))
    {
        My_Log{My_Log::Level::kError} << "embedded file: " << embedded_file << "is invalid\n";
        return qt;
    }

    embedded_raw_buf_ = File::ReadFile(embedded_file);
    qt.v_ = QueryType::EmbeddingQuery;
    return qt;
}

void ModelManager::Clean()
{
    known_model_path_.clear();
    genieModelHandle = nullptr;
    qnn_embedding_info_.Clean();
    embedded_raw_buf_.clear();
}

std::string ModelManager::ResolveKnownModelPath(const std::string &model_feature, bool contain)
{
    static bool config_list_ready{[this]()
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
                                  }()};

    for (const auto config_model_name: config_model_name_list_)
    {
        if (!ModelComparer(config_model_name, model_feature, contain))
        {
            continue;
        }
        return RootDir + "/config/" + config_model_name;
    }

    return "";
}

bool ModelManager::ModelComparer(const std::string &source, const std::string &target, bool contain)
{
    std::string s = source;
    std::string t = target;
    static auto normalize{[](std::string &s)
                          {
                              std::transform(s.begin(), s.end(), s.begin(),
                                             [](unsigned char c) -> unsigned char
                                             {
                                                 if (c == '-' || c == '.' || c == ' ') return '_';
                                                 return static_cast<unsigned char>(std::tolower(c));
                                             });
                          }};

    normalize(s);
    normalize(t);
    return contain ? s.find(t) != std::string::npos : s == t;
}