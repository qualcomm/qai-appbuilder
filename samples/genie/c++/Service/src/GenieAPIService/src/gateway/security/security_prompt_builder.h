//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef SECURITY_PROMPT_BUILDER_H
#define SECURITY_PROMPT_BUILDER_H

#include "../../model/model_config.h"
#include "../../model/model_instance_config.h"
#include "../../processor/harmony.h"
#include <chrono>
#include <iomanip>
#include <ctime>
#include <sstream>
#include <string>

/**
 * 根据模型类型（Harmony 或 General）构建本地模型调用的提示词。
 * 与主推理过程（BuildHarmonyPrompt / BuildPrompt）保持一致：
 *   - Harmony 模型：system_msg + developer_msg + user_msg + "<|start|>assistant<|channel|>final<|message|>" + json_prefill
 *   - 普通模型：j["system"]（替换占位符）+ j["user"]（替换占位符）+ j["start"]
 *
 * @param model_config  模型配置（用于获取 prompt_type 和 prompt_template）
 * @param system_prompt 系统/指令提示词内容（不含格式标记）
 * @param user_message  用户消息内容（不含格式标记）
 * @param json_prefill  [可选] Harmony 格式下 assistant prefill 中预填的 JSON 开始部分
 *                      例如：安全检查传入 "{\"sensitivity\":\""，复杂度检查传入 "{\"complexity\":\""
 *                      空字符串表示不预填 JSON 开始部分（向后兼容）
 * @return 格式化后的完整提示词字符串
 */
inline std::string BuildLocalModelPrompt(
    const IModelConfig* model_config,
    const std::string& system_prompt,
    const std::string& user_message,
    const std::string& json_prefill = "")
{
    if (!model_config)
    {
        // 无模型配置时退化为原始拼接（不应发生）
        return system_prompt + "\n" + user_message;
    }

    // 修复：优先使用 default 模型的 ModelInstanceConfig（多模型场景）。
    // 在多模型场景下，全局 IModelConfig 的 thinking_model_、prompt_type_、prompt_ 等成员
    // 由 LoadSingleModel()（-c 参数指定的模型）设置，而非 default 模型（service_config.json 中的 default_model）。
    // 若 -c 参数指定的是 thinking 模型（如 Qwen3），全局 thinking_model_=true，
    // 会导致 BuildLocalModelPrompt 误判 default 模型（如 Qwen2.0-7B-SSD）为 thinking 模型，
    // 从而在安全检查提示词中错误注入 <think>\n\n</think>\n\n。
    // 通过 GetDefaultInstanceConfig() 获取 default 模型的 ModelInstanceConfig，
    // 确保 is_thinking_model()、get_prompt_type()、get_prompt_template() 读取的是正确的模型配置。
    const ModelInstanceConfig* instance_config = model_config->GetDefaultInstanceConfig();

    // 辅助 lambda：从 instance_config（优先）或 model_config（回退）读取配置
    auto is_thinking  = [&]() -> bool {
        return instance_config ? instance_config->is_thinking_model() : model_config->is_thinking_model();
    };
    auto enable_think = [&]() -> bool {
        return instance_config ? instance_config->getenableThinking() : model_config->getenableThinking();
    };
    auto prompt_type  = [&]() -> PromptType {
        return instance_config ? instance_config->get_prompt_type() : model_config->get_prompt_type();
    };
    auto prompt_tmpl  = [&]() -> const json& {
        return instance_config ? instance_config->get_prompt_template() : model_config->get_prompt_template();
    };

    auto append_think_control = [&](std::string& system_text, std::string& start_prompt) {
        if (!is_thinking()) {
            return;
        }
        if (enable_think()) {
            system_text += "/think";
        } else {
            system_text += "/no_think";
            start_prompt += "<think>\n\n</think>\n\n";
        }
    };

    bool is_harmony = (prompt_type() == PromptType::Harmony);

    if (is_harmony)
    {
        // Harmony 格式：使用 Harmony 特有的提示词标记（与 BuildHarmonyPrompt 保持一致）
        const auto &j = prompt_tmpl();
        std::string knowledge_cutoff = j.value("knowledge_cutoff", "2024-06");

        // 强制 reasoning_level = "low"，禁用高推理（CoT）模式。
        // 安全检查只需要输出一个 JSON 对象，不需要 analysis 通道的推理过程。
        // 使用 "high" 会导致模型先生成数百个 analysis token，严重浪费推理时间。
        std::string reasoning_level = "low";  // j.value("reasoning_level",  "high");

        // 获取当前日期
        auto now_tp    = std::chrono::system_clock::now();
        auto time_t_val = std::chrono::system_clock::to_time_t(now_tp);
        std::tm tm_val  = *std::localtime(&time_t_val);
        std::ostringstream date_oss;
        date_oss << std::put_time(&tm_val, "%Y-%m-%d");
        std::string current_date = date_oss.str();

        std::string system_msg = HarmonyProcessor::BuildSystemMessage(
            knowledge_cutoff, current_date, reasoning_level, false);

        std::string effective_system_prompt = system_prompt;
        std::string ignored_start_prompt;
        append_think_control(effective_system_prompt, ignored_start_prompt);

        std::string developer_msg  = "<|start|>developer<|message|># Instructions\n\n";
        developer_msg += effective_system_prompt;
        developer_msg += "<|end|>";

        std::string user_msg = HarmonyProcessor::BuildUserMessage(user_message);

        // 使用 assistant prefill 技术，预填 final 通道起始标记 + JSON 开始部分，
        // 引导模型跳过 analysis 通道直接输出 JSON 内容。
        // 原理：模型会从我们提供的前缀继续生成，而不是从头开始，
        // 因此不会再生成 <|channel|>analysis<|message|>... 的 CoT 内容。
        //
        // 在 final 通道标记之后预填 JSON 开始部分（由调用方通过 json_prefill 参数指定）：
        //   - 安全检查：{"sensitivity":"  → 模型只需继续生成 S0|S1|S2","reason":"..."}
        //   - 复杂度检查：{"complexity":"  → 模型只需继续生成 C0|C1|C2","reason":"..."}
        // 强制约束模型输出格式，防止模型忽略分类指令而直接回答用户问题。
        // 注意：<|constrain|>json 仅用于工具调用（commentary 通道），不适用于 final 通道。
        return system_msg + developer_msg + user_msg
            + "<|start|>assistant<|channel|>final<|message|>" + json_prefill;
    }
    else
    {
        // 普通模型格式：使用 prompt 模板中的格式（与 BuildPrompt 保持一致）
        // 模板中使用 "string" 作为占位符，替换为实际内容
        const auto &j = prompt_tmpl();

        auto replace_placeholder = [](const std::string& tmpl, const std::string& content) -> std::string
        {
            std::string result = tmpl;
            size_t pos = result.find("string");
            if (pos != std::string::npos)
                result.replace(pos, 6, content);
            return result;
        };

        std::string effective_system_prompt = system_prompt;
        std::string start_prompt = j["start"].get<std::string>();
        append_think_control(effective_system_prompt, start_prompt);

        std::string formatted_system = replace_placeholder(j["system"].get<std::string>(), effective_system_prompt);
        std::string formatted_user   = replace_placeholder(j["user"].get<std::string>(),   user_message);

        // General 模型也支持 json_prefill：在 start_prompt 末尾追加 JSON 开始部分，
        // 引导模型直接输出 JSON 内容，与 Harmony 模型的 assistant prefill 技术效果一致。
        // 例如：安全检查传入 "{\"sensitivity\":\""，复杂度检查传入 "{\"complexity\":\""
        return formatted_system + formatted_user + start_prompt + json_prefill;
    }
}

#endif // SECURITY_PROMPT_BUILDER_H
