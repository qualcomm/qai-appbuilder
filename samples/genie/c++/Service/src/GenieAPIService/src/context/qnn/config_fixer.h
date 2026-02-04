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
            kString,
            kArrayString
        } check_type_;
        bool optional_{};
        bool is_forcast_dir_{false};
    };

    explicit ConfigFixer(const IModelConfig &model_config) : model_config_{model_config} {}

    json Execute();

private:
    bool FixedPath(json &j, FixedInfo &info);

    const IModelConfig &model_config_;
};

using ConfigFixer = GenieContext::ConfigFixer;

inline json ConfigFixer::Execute()
{
    std::ifstream file(model_config_.get_config_path());
    json j;
    file >> j;

    /* @formatter:off */
    std::vector<FixedInfo> fixed_items{
            {"tokenizer", json::json_pointer("/dialog/tokenizer/path"), FixedInfo::kString, false},
            {"extensions", json::json_pointer("/dialog/engine/backend/extensions"), FixedInfo::kString, true},
            {"ctx-bins", json::json_pointer("/dialog/engine/model/binary/ctx-bins"), FixedInfo::kArrayString, false},
            {"forecast", json::json_pointer("/dialog/ssd-q1/forecast-prefix-name"), FixedInfo::kArrayString, true, true},
    };

/* @formatter:on */

    for (auto &fixed_item: fixed_items)
    {
        if (!FixedPath(j, fixed_item))
        {
            throw std::runtime_error("fixed the config path failed");
        }
    }

    if (Genie_getApiMinorVersion() >= 11)
    {
        auto jp = json::json_pointer("/dialog/engine/backend/QnnHtp");
        j.at(jp)["use-mmap"] = true;
        j.at(jp)["allow-async-init"] = true;
    }
    My_Log{} << "fixed the config path successfully\n";
    My_Log{My_Log::Level::kInfo} << j.dump(4) << std::endl;
    return j;
}

inline bool ConfigFixer::FixedPath(json &j, ConfigFixer::FixedInfo &info)
{
    std::string file_path;
    auto get_fixed_path{[&](const json &j) -> bool
                        {
                            bool rs{false};
                            std::string file_name;

                            if (!j.is_string())
                            {
                                My_Log{} << info.item_str_ << " json object is not string\n";
                                goto done;
                            }

                            if (info.is_forcast_dir_)
                            {
                                file_path = model_config_.get_model_path();
                                rs = true;
                                goto done;
                            }

                            file_name = fs::path{j.get<std::string>()}.filename().generic_string();
                            if (file_name.empty())
                            {
                                My_Log{} << info.item_str_ << " json object file name is not empty\n";
                                goto done;
                            }

                            file_path = model_config_.get_model_path() + "/" + file_name;
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
