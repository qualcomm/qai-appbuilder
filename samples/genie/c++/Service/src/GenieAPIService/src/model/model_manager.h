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

class ModelManager : public IModelConfig
{
public:
    explicit ModelManager(IModelConfig &&config);

    bool LoadModelByName(const std::string &new_model, bool &first_load);

    bool InitializeConfig(bool load);

    void UnloadModel();

    bool IsLoaded()
    { return loaded_; }

private:
    bool LoadModel();

    PromptType LoadPromptTemplates(std::string &&prompt_path);

    std::string ResolveKnownModelPath(const std::string& model_feature, bool only_prefix);

    void Clean();

    static bool ModelComparer(const std::string &source, const std::string &target, bool only_prefix);

    struct ModeVerifier;

    class QNNImpl;
    
    std::atomic<bool> loaded_{false};
};

#endif //MODEL_MANAGER_H
