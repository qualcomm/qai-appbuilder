//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef MODEL_CONFIG_H
#define MODEL_CONFIG_H

#include "def.h"
#include <nlohmann/json.hpp>

using json = nlohmann::ordered_json;

class ContextBase;

class IModelConfig
{
public:
    ~IModelConfig() = default;

    const std::string &get_config_path() const
    {
        return config_file_;
    }

    const json &get_prompt_template() const
    {
        return prompt_;
    }

    int &context_size() const
    {
        return context_size_;
    }

    const std::string &get_model_path() const
    {
        return model_path_;
    }

    const std::string &get_model_name() const
    {
        return model_name_;
    }

    const std::string &getloraAdapter() const
    {
        return loraAdapter;
    }

    bool getisOutputAllText() const
    {
        return outputAllText;
    }

    bool getenableThinking() const
    {
        return enableThinking;
    }

    int getnumResponse() const
    {
        return numResponse;
    }

    int getminOutputNum() const
    {
        return minOutputNum;
    }

    float getloraAlpha() const
    {
        return loraAlpha;
    }

    json get_model_list() const;

    const QNNEmbedding &get_qnn_embedding() const
    {
        return qnn_embedding_;
    }

    PromptType get_prompt_type() const
    {
        return prompt_type_;
    }

    bool is_thinking_model() const
    {
        return thinking_model_;
    }

    std::weak_ptr<ContextBase> get_genie_model_handle()
    {
        return genieModelHandle;
    }

protected:
    std::shared_ptr<ContextBase> genieModelHandle{};
    std::string model_root_;
    std::string model_path_;
    std::string model_name_;
    std::string known_model_path_;
    std::vector<std::string> config_model_name_list_;
    mutable std::vector<std::string> model_list_;
    mutable int context_size_{DEFAULT_CONTEXT_SIZE};
    json prompt_;
    bool thinking_model_{false};
    PromptType prompt_type_{};
    ModelFormat model_format_{};

    std::string config_file_;
    std::string loraAdapter = "default_adapter";
    bool outputAllText = false;
    bool enableThinking = false;
    int numResponse = 30;
    int minOutputNum = 1024;
    float loraAlpha = 0.5;
    QNNEmbedding qnn_embedding_;

    void UpdateModeList() const;

    friend class Config;
};

#endif //MODEL_CONFIG_H
