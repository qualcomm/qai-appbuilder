#ifndef GENIEAPICLIENT_SLN_MODEL_CONFIG_H
#define GENIEAPICLIENT_SLN_MODEL_CONFIG_H

#include "def.h"
#include <string>
#include <nlohmann/json.hpp>

using json = nlohmann::ordered_json;

class ContextBase;

class IModelConfig
{
public:
    ~IModelConfig() = default;

    const std::string &get_config_path() const
    { return config_file_; }

    const json &get_prompt_template() const
    { return prompt_; }

    int &context_size() const
    { return contextSize; };

    const std::string &get_model_path() const
    { return model_path; }

    const std::string &getModelName() const
    { return model_name_; };

    const std::string &getloraAdapter() const
    { return loraAdapter; }

    bool getisOutputAllText() const
    { return outputAllText; }

    bool getenableThinking() const
    { return enableThinking; }

    int getnumResponse() const
    { return numResponse; }

    int getminOutputNum() const
    { return minOutputNum; }

    float getloraAlpha() const
    { return loraAlpha; }

    json get_model_list() const;

    QueryType get_query_type() const
    { return query_type_; }

    const std::vector<uint8_t> &get_query_embedded_buffer() const
    { return embedded_buffer_; }

    ModelStyle get_model_style() const
    { return model_style_; }

    std::weak_ptr<ContextBase> get_genie_model_handle()
    { return genieModelHandle; };

protected:
    std::shared_ptr<ContextBase> genieModelHandle;
    std::string model_root_;
    std::string model_path;
    std::string model_name_;
    mutable std::vector<std::string> model_list_;
    mutable int contextSize{4096};
    json prompt_;
    ModelStyle model_style_{};
    ModelType model_type_{};

    std::string config_file_, prompt_path_;
    QueryType query_type_;
    std::string loraAdapter = "default_adapter";
    bool outputAllText = false;
    bool enableThinking = false;
    int numResponse = 30;
    int minOutputNum = 1024;
    float loraAlpha = 0.5;
    std::vector<uint8_t> embedded_buffer_;

    void UpdateModeList() const;

    friend class Config;
};

#endif //GENIEAPICLIENT_SLN_MODEL_CONFIG_H
