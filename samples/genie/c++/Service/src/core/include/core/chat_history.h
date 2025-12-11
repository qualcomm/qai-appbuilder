#ifndef GENIEAPICLIENT_SLN_CHAT_HISTORY_H
#define GENIEAPICLIENT_SLN_CHAT_HISTORY_H

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

#endif //GENIEAPICLIENT_SLN_CHAT_HISTORY_H
