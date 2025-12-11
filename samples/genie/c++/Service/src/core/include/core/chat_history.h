//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef CHAT_HISTORY_H
#define CHAT_HISTORY_H

#include <string>
#include "nlohmann/json.hpp"
#include "core/model/model_config.h"

using json = nlohmann::ordered_json;

class ChatHistory
{
public:
    ChatHistory(IModelConfig &model_config_);

    ~ChatHistory();

    std::string BuildPrompt(json &data, bool& is_tool);

    void AddMessage(const std::string &role, const std::string &content);

    void Print() const;

    void Limit(size_t max_size);

    void Clear();

    bool import_from_json(const nlohmann::json &j);

    nlohmann::json export_to_json() const;

private:
    class Impl;
    Impl* impl_{};
};

#endif //CHAT_HISTORY_H
