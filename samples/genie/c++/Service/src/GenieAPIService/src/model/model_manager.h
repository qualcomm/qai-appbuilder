//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef MODEL_MANAGER_H
#define MODEL_MANAGER_H

#include "model_config.h"
#include "model_instance_config.h"
#include <memory>
#include <mutex>
#include <atomic>
#include <unordered_map>
#include <vector>
#include <string>

class ContextBase;

struct LoadedModel
{
    std::shared_ptr<ModelInstanceConfig> config;
    std::shared_ptr<ContextBase> context;
    std::string backend;
    std::string device;
    std::vector<std::string> occupied_families;
    bool is_loaded{false};
};

class ModelManager : public IModelConfig
{
public:
    explicit ModelManager(IModelConfig &&config);

    ~ModelManager() override;

    bool LoadModelByName(const std::string &new_model, bool &first_load);
    bool InitializeConfig();
    void UnloadModel();

    bool IsLoaded()
    {
        return loaded_.load();
    }

    bool IsLocalModelAvailable() const override
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        return !loaded_models_.empty();
    }

    std::weak_ptr<ContextBase> GetDefaultModelHandle() const override
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        auto model = GetDefaultModelLocked();
        if (model && model->context) {
            return model->context;
        }
        return genieModelHandle;
    }

    ModelInstanceConfig* GetDefaultInstanceConfig() const override
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        auto model = GetDefaultModelLocked();
        if (model && model->config) {
            return model->config.get();
        }
        return nullptr;
    }

    std::shared_ptr<LoadedModel> GetModel(const std::string &model_name);

    std::shared_ptr<LoadedModel> GetDefaultModel();

    std::vector<std::string> ListLoadedModels() const;

    std::vector<json> ScanModelDirectory() const;

    bool LoadSingleModel();

    enum class LoadFailureReason
    {
        kNone,
        kInsufficientMemory,
        kOther
    };

    void SetLastLoadFailureReason(LoadFailureReason reason, const std::string &detail)
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        last_load_failure_reason_ = reason;
        last_load_failure_detail_ = detail;
    }

    LoadFailureReason GetLastLoadFailureReason() const
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        return last_load_failure_reason_;
    }

    std::string GetLastLoadFailureDetail() const
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        return last_load_failure_detail_;
    }

private:

    std::shared_ptr<ContextBase> CreateContext(
        const std::shared_ptr<ModelInstanceConfig> &config,
        const std::string &backend);

    PromptType LoadPromptTemplates(std::string &&prompt_path);
    std::string ResolveKnownModelPath(const std::string& model_feature, bool only_prefix);
    void Clean();
    static bool ModelComparer(const std::string &source, const std::string &target, bool only_prefix);

    struct ModeVerifier;
    class QNNImpl;

    uint64_t EstimateOtherLoadedModelsMemoryBytes() const;

    std::shared_ptr<LoadedModel> GetDefaultModelLocked() const;
    void UnloadModelsByDevice(const std::vector<std::string> &families);
    static std::vector<std::string> ComputeOccupiedFamilies(const LoadedModel &model);
    static std::vector<std::string> ResolveTentativeFamilies(const std::string &model_path,
                                                              const std::string &config_file);

    std::unordered_map<std::string, std::shared_ptr<LoadedModel>> loaded_models_;
    std::string default_model_name_;
    mutable std::mutex models_mutex_;

    std::atomic<bool> loaded_{false};

    LoadFailureReason last_load_failure_reason_{LoadFailureReason::kNone};
    std::string last_load_failure_detail_;
};

#endif //MODEL_MANAGER_H
