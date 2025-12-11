//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include <core/model/model_config.h>
#include "core/log.h"
#include "core/utils.h"
#include "core/model/model_manager.h"
#include "../../context/genie.h"
#include "../../context/mnn.h"
#include "../../context/llama_cpp.h"
#include <filesystem>

namespace fs = std::filesystem;

struct ModelManager::ModeInfo
{
    static bool isHarmonyStyleModel(ModelManager *self)
    {
        return str_contains(self->model_name_, "harmony") ||
               str_contains(self->model_name_, "GPT") ||
               str_contains(self->model_name_, "OSS") ||
               str_contains(self->prompt_["assistant"], "<|channel|>");
    }

    class ModeVerify
    {
    public:
        explicit ModeVerify(ModelManager *self) : self_{self}
        {}

    protected:
        static bool PreCheck(ModelManager *self, const std::string &ext)
        {
            for (const auto &entry: fs::directory_iterator(self->model_path))
            {
                if (entry.is_regular_file() && entry.path().extension() == ext)
                {
                    return true;
                }
            }
            return false;
        }

        ModelManager *self_;

    private:
        virtual std::shared_ptr<ContextBase> CreateIfVerifiedImpl() = 0;
    };

    struct QnnVerify : public ModeVerify
    {
        explicit QnnVerify(ModelManager *self) : ModeVerify(self)
        {}

        std::shared_ptr<ContextBase> CreateIfVerifiedImpl() override
        {
            if (!ModeVerify::PreCheck(self_, ".bin"))
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
            return std::make_shared<GenieContext>(*self_);
        }
    };

    struct MnnVerify : public ModeVerify
    {
        explicit MnnVerify(ModelManager *self) : ModeVerify(self)
        {}

        std::shared_ptr<ContextBase> CreateIfVerifiedImpl() override
        {
            if (!ModeVerify::PreCheck(self_, ".mnn"))
            {
                return nullptr;
            }
            return std::make_shared<MNNContext>(*self_);
        }
    };

    struct GGUFVerify : public ModeVerify
    {
        explicit GGUFVerify(ModelManager *self) : ModeVerify(self)
        {}

        std::shared_ptr<ContextBase> CreateIfVerifiedImpl() override
        {
            if (!ModeVerify::PreCheck(self_, ".gguf"))
            {
                return nullptr;
            }
            return std::make_shared<LLAMACppBuilder>(*self_);
        }
    };

    static std::shared_ptr<ContextBase> GuessAndCreateModel(ModelManager *self)
    {
        struct ModelInfo
        {
            ModelType model_type_;
            std::function<std::shared_ptr<ContextBase>()> func_;
        };

        /* @formatter:off */
        std::vector<ModelInfo> models{
                {{ModelType::QNN}, [self](){ return QnnVerify(self).CreateIfVerifiedImpl();}},
#ifdef USE_MNN
                {{ModelType::MNN}, [self](){ return MnnVerify(self).CreateIfVerifiedImpl();}},
#endif
#ifdef USE_GGUF
                {{ModelType::GGUF},[self](){ return GGUFVerify(self).CreateIfVerifiedImpl();}}
#endif
        };
        /* @formatter:on */

        std::shared_ptr<ContextBase> context;

        try
        {
            for (const auto &model: models)
            {
                My_Log{} << "try to check if is: "
                         << const_cast<ModelType &>(model.model_type_).to_string()
                         << " model\n";

                auto use_second = MeasureSeconds(
                        [&context, &model]()
                        {
                            context = model.func_();
                        });

                if (context)
                {
                    My_Log{} << "load successfully! use second: " << use_second << " \n";
                    self->model_type_ = model.model_type_;
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

bool ModelManager::LoadModelByName(const std::string &model_name, bool &new_model)
{
    if (model_root_.empty())
    {
        My_Log{My_Log::Level::kError}
                << "you have not load by config file yet, this is invalid operation: " << model_name
                << std::endl;
        return false;
    }

    if (model_name.empty())
    {
        My_Log{My_Log::Level::kError} << "model name can not be empty" << std::endl;
        return false;
    }

    if (model_name_ == model_name & loaded_)
    {
        My_Log{} << "model: " + model_name << " already been loaded" << std::endl;
        new_model = false;
        return true;
    }

    My_Log{} << "ModelManager::loadModel,modelName=" + model_name << std::endl;
    new_model = true;

    UpdateModeList();
    bool found{false};
    for (const auto &name: model_list_)
    {
        if (str_search(name, model_name))
        {
            found = true;
            model_name_ = name;
            model_path = model_root_ + "/" + model_name;
            config_file_ = model_path + "/config.json";
            break;
        }
    }

    if (!found)
    {
        My_Log{My_Log::Level::kError} << "model name: " << model_name << " is not exist" << std::endl;
        return false;
    }

    if (!fs::exists(config_file_))
    {
        My_Log{My_Log::Level::kError} << "Config file not found: " << config_file_ << std::endl;
        return false;
    }

    genieModelHandle = nullptr;
    loaded_ = false;

    return loadModel();
}

bool ModelManager::InitializeConfig(bool load)
{
    fs::path config_path{config_file_};
    My_Log{} << "ModelManager::loadModel,configFile=" + config_path.generic_string() << std::endl;

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
        model_path = check_path(config_path.parent_path().generic_string());
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

    return loadModel();
}

bool ModelManager::loadModel()
{
    // Check if prompt.json exists
    prompt_path_ = model_path + "/prompt.json";
    if (!LoadPromptTemplates(prompt_path_))
    {
        My_Log{} << model_name_ << ": prompt.json not found " << std::endl;
        return false;
    }

    genieModelHandle = ModeInfo::GuessAndCreateModel(this);
    if (!genieModelHandle)
    {
        My_Log{} << RED << "Load Model Failed, Model Name: " << model_name_ << RESET << std::endl;
        return false;
    }

    model_style_ = check_model_style();
    My_Log{} << "check model style is " << model_style_.to_string() << "\n";

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
        genieModelHandle = nullptr;
    }

    model_name_ = "";
}

bool ModelManager::LoadPromptTemplates(const std::string &promptPath)
{
    std::ifstream file(promptPath);
    if (!file.is_open())
    {
        My_Log{} << "ERR: Cannot open prompt file: " << promptPath << std::endl;
        return false;
    }
    json j;

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

        if (contextSize < 0)
        {
            try
            {
                if (j.contains("context_size"))
                {
                    contextSize = j["context_size"].get<int>();
                }
                else
                {
                    contextSize = CONTEXT_SIZE;
                }
            }
            catch (const std::exception &e)
            {
                My_Log{} << "ERR: Failed to Process JSON: " << e.what() << std::endl;
                contextSize = CONTEXT_SIZE;
            }
        }
    }
    catch (const std::exception &e)
    {
        My_Log{} << "ERR: Failed to Process JSON: " << e.what() << std::endl;
        return false;
    }

    return true;
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

ModelStyle ModelManager::check_model_style()
{
    ModelStyle ms;
    std::unordered_map<int, bool (*)(ModelManager *)> models_style{
            {{ModelStyle::Harmony}, ModeInfo::isHarmonyStyleModel}
    };

    for (auto &model_style: models_style)
    {
        if (model_style.second(this))
        {
            ms.v_ = model_style.first;
            break;
        }
    }
    return ms;
}
