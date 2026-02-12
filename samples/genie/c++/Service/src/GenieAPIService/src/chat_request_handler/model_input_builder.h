//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef PROMPT_H
#define PROMPT_H

#include "../context/context_base.h"
#include "../chat_history/chat_history.h"
#include <utils.h>
#include "log.h"
#include <nlohmann/json.hpp>

using json = nlohmann::ordered_json;

// TODO: bad impl!
class ModelInputBuilder
{
public:
    ModelInputBuilder(ChatHistory &chat_history, IModelConfig &model_config) : chat_history_{chat_history},
                                                                               model_config_{model_config} {}

    ModelInput &Build(json &data, bool &is_tool)
    {
        Reset();
        for (const auto &e: data["messages"])
        {
            if (e["role"] == "user")
            {
                const json user_content = e["content"];
                if (user_content.is_string())
                {
                    model_input_.text_ = user_content;
                }
                else if (user_content.is_array())
                {
                    ProcessArray(user_content);
                }
                else if (user_content.is_object())
                {
                    ProcessObject(user_content);
                }
                else
                {
                    throw ReportError{"user content is not a object or array"};
                }
            }
        }

        if (model_input_.text_.empty())
        {
            throw ReportError{"question key can not be empty"};
        }

        if (!model_input_.image_.empty() || !model_input_.audio_.empty())
        {
            goto done;
        }

        model_input_.text_ = BuildPrompt(data, is_tool);
        if (model_input_.text_.empty())
        {
            throw ReportError{"build prompt failed"};
        }

        done:
        return model_input_;
    }

private:
    void ProcessArray(const json &user_content)
    {
        static auto check_key{
                [](const json &content, const char *key, bool is_object = false) -> bool
                {
                    bool invalid;
                    if (!content.contains(key))
                    {
                        goto error;
                    }

                    invalid = is_object ? !content[key].is_object() : !content[key].is_string();
                    if (invalid)
                    {
                        goto error;
                    }

                    return true;
                    error:
                    My_Log{} << "user content " << key << " key is invalid\n";
                    return false;
                }
        };

        for (const auto &element: user_content)
        {
            if (!check_key(element, "type"))
            {
                continue;
            }

            if (strcmp(element["type"].get_ref<const std::string &>().c_str(), "text") == 0)
            {
                if (!check_key(element, "text"))
                {
                    continue;
                }
                model_input_.text_ = element["text"].get<std::string>();
            }

            if (strcmp(element["type"].get_ref<const std::string &>().c_str(), "image_url") == 0)
            {
                if (!check_key(element, "image_url", true))
                {
                    continue;
                }

                auto &j_image_url = element["image_url"];
                if (!check_key(j_image_url, "url"))
                {
                    continue;
                }

                auto img = j_image_url["url"].get<std::string>();
                int pos;
                if ((pos = img.find(',')) != std::string::npos)
                {
                    ++pos;
                    model_input_.image_ = img.substr(pos, img.size() - pos);
                }
            }
        }
    }

    void ProcessObject(const json &user_content)
    {
        static auto get_value{
                [](const json &content, const char *key) -> std::string
                {
                    if (!content.contains(key))
                    {
                        My_Log{} << "msg does not contain " << key << " key\n";
                        return "";
                    }

                    if (!content[key].is_string())
                    {
                        throw ReportError{std::string{key} + " key is invalid"};
                    }

                    return content[key].get_ref<const std::string &>();
                }
        };

        model_input_ = ModelInput{
                get_value(user_content, "question"),
                get_value(user_content, "image"),
                get_value(user_content, "audio"),
        };
    }

    std::string BuildPrompt(const json &data, bool &is_tool)
    {
        is_tool = false;
        // parse data
        const json msg = data["messages"];
        const json tools = data["tools"];
        std::string userToolsPrompt; // Tool description sent by client
        std::string systemDefaultPrompt = "You are a helpful assistant.";
        std::string startDefaultPrompt;
        std::string userContent; // question sent by client
        std::string userToolCallBackResult; // Client tool call result
        auto handle = model_config_.get_genie_model_handle().lock();
        auto &context_size = model_config_.context_size();

        // add tool description
        if (tools != nullptr && !tools.empty())
        {
            is_tool = true;
            chat_history_.Clear();
            // My_Log{} << "Tools model, clear chat history." << msg << std::endl;

            size_t toolsLength = 0;
            for (const auto &element: tools)
            {
                std::string userToolPrompt = json_to_str(element);
                size_t tool_length = handle->TokenLength(userToolPrompt);
                if (toolsLength + tool_length < context_size - model_config_.getminOutputNum())
                {
                    userToolsPrompt += userToolPrompt + "\n";
                    toolsLength += tool_length;
                }
                else
                {
                    My_Log{} << "Tool is too long and will be truncated, "
                             << "toolsLength: " << toolsLength << " "
                             << "tool_length: " << tool_length << std::endl;
                }
            }
            userToolsPrompt = str_replace(tool_prompt_template, "{tool_descs}", userToolsPrompt);
        }

        // Extract the content sent by the client, including the user's question, system prompts, and tool invocation results.
        userContent = model_input_.text_;
        chat_history_.AddMessage("user", userContent);
        for (const auto &element: msg)
        {
            // TODO: Handle history message.
            auto role = get_json_value(element, "role", BLANK_STRING);
            if (role == "system")
            {
                const json system_content = element["content"];
                if (system_content.is_array())
                {
                    for (const auto &sys_element: system_content)
                    {
                        if (strcmp(sys_element["type"].get_ref<const std::string &>().c_str(), "text") == 0)
                        {
                            systemDefaultPrompt = sys_element["text"].get_ref<const std::string &>();
                            break;
                        }
                    }
                }
                else if (system_content.is_object())
                {
                    systemDefaultPrompt = get_json_value(element, "content", BLANK_STRING);
                }
                systemDefaultPrompt = str_replace(systemDefaultPrompt, "\\n", "\n");
            }
            else if (role == "tool")
            {
                // TODO: Check tool id.
                userToolCallBackResult += trim_empty_lines(get_json_value(element, "content", BLANK_STRING));
                userToolCallBackResult = "<tool_response>\n" + userToolCallBackResult + "\n</tool_response>\n";
                chat_history_.AddMessage("tool", userToolCallBackResult);
            }
        }

        // build system prompt
        systemDefaultPrompt += userToolsPrompt;

        // If it is a thinkable model, change the system prompt.
        if (model_config_.is_thinking_model())
        {
            if (model_config_.getenableThinking())
            {
                systemDefaultPrompt += "/think";
            }
            else
            {
                systemDefaultPrompt += "/no_think";
                startDefaultPrompt += FILL_THINK;
            }
        }

        // build model input
        auto &j = model_config_.get_prompt_template();
        /* @formatter:off */
        std::string modelInputContent = chat_history_.GetUserMessage(str_replace(j["system"].get_ref<const std::string&>(), "string", systemDefaultPrompt),
                                                                     j["start"].get_ref<const std::string&>() + startDefaultPrompt);
        /* @formatter:on */
        return modelInputContent;
    }

    std::string trim_empty_lines(const std::string &input)
    {
        std::string s = input;
        s = std::regex_replace(s, std::regex(R"(^(\s*\n)+)"), "");
        s = std::regex_replace(s, std::regex(R"((\s*\n)+$)"), "");
        return s;
    }

    void Reset()
    {
        model_input_.text_.clear();
        model_input_.image_.clear();
        model_input_.audio_.clear();
    }

    static inline const std::string FILL_THINK = "<think>\n\n</think>\n\n";

    static inline const std::string BLANK_STRING;

    static inline const std::string tool_prompt_template =
            "\n\n# Tools\n\n"
            "You may call one or more functions to assist with the user query.\n\n"
            "You are provided with function signatures within <tools></tools> XML tags:\n"
            "<tools>\n"
            "{tool_descs}"
            "</tools>\n\n"
            "For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:\n"
            "<tool_call>\n"
            "{\"name\": <function-name>, \"arguments\": <args-json-object>}\n"
            "</tool_call>\n";

    ChatHistory &chat_history_;
    IModelConfig &model_config_;
    ModelInput model_input_;
};

#endif //PROMPT_H
