//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "llama_speculative_config.h"

#ifdef _WIN32
#include <windows.h>
#else
#include <sys/sysinfo.h>
#endif

namespace llama_speculative
{

DraftModelConfig DraftModelConfig::ParseFrom(const json &model_config_json)
{
    DraftModelConfig result;

    if (!model_config_json.is_object() || !model_config_json.contains("draft_model"))
    {
        return result;
    }

    const json &block = model_config_json.at("draft_model");
    if (!block.is_object())
    {
        return result;
    }

    result.present = true;

    if (block.contains("path") && block["path"].is_string())
    {
        result.path = block["path"].get<std::string>();
    }

    if (block.contains("spec_type") && block["spec_type"].is_string())
    {
        result.spec_type = block["spec_type"].get<std::string>();
    }

    if (block.contains("spec_draft_n_max") && block["spec_draft_n_max"].is_number())
    {
        result.spec_draft_n_max = block["spec_draft_n_max"].get<int>();
    }

    if (block.contains("spec_draft_p_min") && block["spec_draft_p_min"].is_number())
    {
        result.spec_draft_p_min = block["spec_draft_p_min"].get<double>();
    }

    // required 默认 true：drafter 加载失败则整个模型加载失败，不静默降级为纯 target 解码。
    if (block.contains("required") && block["required"].is_boolean())
    {
        result.required = block["required"].get<bool>();
    }

    return result;
}

uint64_t GetTotalPhysicalMemoryBytes()
{
#ifdef _WIN32
    MEMORYSTATUSEX statex;
    statex.dwLength = sizeof(statex);
    if (GlobalMemoryStatusEx(&statex))
    {
        return statex.ullTotalPhys;
    }
#else
    struct sysinfo info{};
    if (sysinfo(&info) == 0)
    {
        return static_cast<uint64_t>(info.totalram) * info.mem_unit;
    }
#endif
    return UINT64_MAX;
}

} // namespace llama_speculative
