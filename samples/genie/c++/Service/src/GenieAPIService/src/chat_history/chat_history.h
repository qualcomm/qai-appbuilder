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
#include <memory>
#include "nlohmann/json.hpp"

using json = nlohmann::ordered_json;
struct GenieChatMessage
{
    std::string role;        // role："user", "assistant", "tool"
    std::string content;     // message content
};

class ModelInstanceConfig;
class IModelConfig;

class ChatHistory
{
public:
    explicit ChatHistory(ModelInstanceConfig &model_config);
    
    explicit ChatHistory(IModelConfig &model_config);

    ~ChatHistory();

    std::string GetUserMessage(const std::string &prompt_system,
                               const std::string &prompt_start);

    void AddMessage(const std::string &role, const std::string &content);

    void Print() const;

    void Limit(size_t max_size);

    void Clear();

    bool import_from_json(const json &j);

    json export_to_json() const;

private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};

#endif //CHAT_HISTORY_H
