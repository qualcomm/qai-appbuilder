//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef CONFIG_FIXER_H
#define CONFIG_FIXER_H

#include "genie.h"
#include "../../model/model_config.h"

#include <nlohmann/json.hpp>

using json = nlohmann::ordered_json;

#include <filesystem>

namespace fs = std::filesystem;

class GenieContext::ConfigFixer
{
public:
    struct FixedInfo
    {
        const char *item_str_{};
        json::json_pointer jp_;
        enum CheckType
        {
            kBool,
            kString,
            kArrayString
        } check_type_;
        bool optional_{};
        bool additional_{false};  // for string will set to model_root, for bool valye is true or false
    };

    explicit ConfigFixer(const IModelConfig &model_config) : model_config_{model_config}
    {
        const std::string &config_path = model_config_.get_config_path();

        // Trim leading whitespace to check if it's JSON
        std::string trimmed_config = config_path;
        size_t start = trimmed_config.find_first_not_of(" \t\n\r");
        if (start != std::string::npos)
        {
            trimmed_config = trimmed_config.substr(start);
        }

        // Check if config_path is a JSON string (starts with '{')
        if (!trimmed_config.empty() && trimmed_config[0] == '{')
        {
            // Parse JSON string directly
            try
            {
                j_ = json::parse(config_path);
                My_Log{My_Log::Level::kInfo} << "Parsed config from JSON string\n";
            } catch (const std::exception &e)
            {
                My_Log{My_Log::Level::kError} << "Failed to parse JSON config in ConfigFixer: " << e.what() << "\n";
                throw;
            }
        }
        else
        {
            // Read from file
            std::ifstream file(config_path);
            if (!file.good())
            {
                My_Log{My_Log::Level::kError} << "Cannot open config file in ConfigFixer: " << config_path << "\n";
                throw std::runtime_error("Cannot open config file: " + config_path);
            }
            file >> j_;
            My_Log{My_Log::Level::kInfo} << "Loaded config from file: " << config_path << "\n";
        }

        My_Log{My_Log::Level::kInfo} << j_.dump(4) << "\n";
    }

    json FixConfig();

    json FixSampler() const
    {
        json j_config = j_["dialog"]["sampler"];
        json j_sampler_root{{"sampler", {}}};
        json &j_out_sampler = j_sampler_root["sampler"];

        for (auto &key: {"version", "seed", "temp", "top-k", "top-p"})
        {
            model_config_.sampler()[key] = "";
            if (j_config.contains(key))
            {
                j_out_sampler[key] = j_config[key];
                model_config_.sampler()[key] = j_config[key];
            }
        }
        j_out_sampler["type"] = "basic";
        My_Log{My_Log::Level::kInfo} << "sampler text: \n" << j_sampler_root.dump(4) << std::endl;
        return j_sampler_root;
    }

private:
    bool FixedPath(json &j, FixedInfo &info);

    const IModelConfig &model_config_;

    json j_;
};

using ConfigFixer = GenieContext::ConfigFixer;

inline json ConfigFixer::FixConfig()
{
    /* @formatter:off */
    std::vector<FixedInfo> fixed_items{
            {"tokenizer", json::json_pointer("/dialog/tokenizer/path"), FixedInfo::kString, false},
            {"extensions", json::json_pointer("/dialog/engine/backend/extensions"), FixedInfo::kString, true},
            {"ctx-bins", json::json_pointer("/dialog/engine/model/binary/ctx-bins"), FixedInfo::kArrayString, false},
            {"forecast", json::json_pointer("/dialog/ssd-q1/forecast-prefix-name"), FixedInfo::kString, true, true},
            {"poll", json::json_pointer("/dialog/engine/backend/QnnHtp/poll"),FixedInfo::kBool, true, false}
    };

/* @formatter:on */

    for (auto &fixed_item: fixed_items)
    {
        if (!FixedPath(j_, fixed_item))
        {
            throw std::runtime_error("fixed the config path failed");
        }
    }

    if (Genie_getApiMinorVersion() >= 11)
    {
        auto jp = json::json_pointer("/dialog/engine/backend/QnnHtp");
        j_.at(jp)["use-mmap"] = true;
        j_.at(jp)["allow-async-init"] = true;
    }

    My_Log{} << "fixed the config path successfully\n";
    My_Log{My_Log::Level::kInfo} << j_.dump(4) << std::endl;
    return j_;
}

inline bool ConfigFixer::FixedPath(json &j, ConfigFixer::FixedInfo &info)
{
    std::string file_path;
    auto get_fixed_path{[&](const json &j) -> bool
                        {
                            bool rs{false};
                            std::string file_name;
                            fs::path current_path;
                            std::string current_path_str;

                            if (info.additional_)
                            {
                                file_path = model_config_.get_model_path();
                                rs = true;
                                goto done;
                            }

                            if (!j.is_string())
                            {
                                My_Log{} << info.item_str_ << " json object is not string\n";
                                goto done;
                            }

                            if (j.empty())
                            {
                                My_Log{} << info.item_str_ << " json object file name is  empty\n";
                                goto done;
                            }

                            current_path = fs::path{j.get_ref<const std::string &>()};
                            file_path = current_path.generic_string();
                            if (!current_path.is_absolute())
                            {
                                file_path = model_config_.get_model_path() + "/" + file_path;
                            }

                            if (File::IsFileExist(file_path))
                            {
                                rs = true;
                                goto done;
                            }

                            My_Log{} << "file path: " << current_path_str << " is not exist, will check: ";

                            file_name = current_path.filename().generic_string();
                            file_path = model_config_.get_model_path() + "/" + file_name;
                            My_Log{}.original(true) << file_path << "\n";
                            if (File::IsFileExist(file_path))
                            {
                                rs = true;
                                goto done;
                            }

                            My_Log{} << "file path: " << file_path << " is not exist, will use default ver: ";
                            file_path = RootDir + "/" + file_name;
                            My_Log{}.original(true) << file_path << "\n";

                            if (!File::IsFileExist(file_path))
                            {
                                My_Log{} << "file path: " << file_path << " is not exist\n";
                                goto done;
                            }

                            rs = true;
                            done:
                            return rs;
                        }};

    if (!j.contains(info.jp_))
    {
        My_Log{} << info.item_str_ << " json object is not found\n";
        if (!info.optional_)
        {
            return false;
        }
        return true;
    }

    switch (info.check_type_)
    {
        case FixedInfo::kBool:
            j.at(info.jp_) = info.additional_;
            break;
        case FixedInfo::kString:
            if (get_fixed_path(j.at(info.jp_)))
            {
                j.at(info.jp_) = file_path;
                return true;
            }
            if (info.optional_)
                j.at(info.jp_.parent_pointer()).erase(info.jp_.back());
            else
                return false;
            break;
        case FixedInfo::kArrayString:
            for (auto &item: j.at(info.jp_))
            {
                if (!get_fixed_path(item) && !info.optional_)
                {
                    return false;
                }
                item = file_path;
            }
            break;
    }
    return true;
}

#endif //CONFIG_FIXER_H
