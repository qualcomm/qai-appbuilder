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
    std::string role;
    std::string content;
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
                               const std::string &prompt_start,
                               const std::string &assistant_turn_prefix = "",
                               const std::string &trailing_note = "");

    void AddMessage(const std::string &role, const std::string &content);

    void PrependToLastMessage(const std::string &prefix);

    bool IsEmpty() const;

    void Print() const;

    void Limit(size_t max_size);

    void Clear();

    bool import_from_json(const json &j);

    json export_to_json() const;

private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};

#endif
