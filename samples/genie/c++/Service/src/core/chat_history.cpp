//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "core/chat_history.h"
#include "core/utils.h"
#include "core/log.h"
#include "core/context_base.h"

class ChatHistory::Impl
{
public:
    struct GenieChatMessage
    {
        std::string role;     // role："user", "assistant", "tool"
        std::string content;  // message content
    };

    explicit Impl(IModelConfig &model_config) : model_config_{model_config}
    {}

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

/*
"Here is a sample:\n";
"<tool_call>\n"
"{\"name\": \"example_function\", \"arguments\": {\"arg1\": \"value1\", \"arg2\": \"value2\"}}\n"
"</tool_call>\n";
*/
    const std::vector<GenieChatMessage> &get() const
    { return history; }

    std::string get_user_message(size_t num_response,
                                 const std::string &prompt_system,
                                 const std::string &prompt_start,
                                 int contextSize)
    {
        std::string res;
        std::vector<GenieChatMessage> user_message_vector;

        // Ensure the tool calls are proceeding normally.
        size_t keep_content_num = 2 * num_response + 1;
        if (num_response <= 1 && need_force_tool_context())
        {
            keep_content_num = 4;
        }

        size_t count = history.size();
        for (size_t i = 0; i < count && i < keep_content_num; i++)
        {
            user_message_vector.push_back(get_message(count - i - 1));
        }

        res = data_process_strategy(user_message_vector, prompt_system, prompt_start, contextSize);

        return res;
    }

    std::string data_process_strategy(std::vector<GenieChatMessage> &user_message_vector,
                                      const std::string &prompt_system,
                                      const std::string &prompt_start,
                                      int contextSize);

    void add_message(const std::string &role, const std::string &content)
    { history.emplace_back(GenieChatMessage{role, content}); }

    const GenieChatMessage &get_message(size_t index) const
    {
        if (index >= history.size())
        {
            throw std::out_of_range("ChatHistory: Index out of range");
        }
        return history.at(index);
    }

    bool need_force_tool_context() const
    {
        size_t count = history.size();
        if (get_message(count - 1).role == "tool")
        {
            return true;
        }
        return false;
    };

    IModelConfig &model_config_;

    std::vector<GenieChatMessage> history;
};

std::string ChatHistory::Impl::data_process_strategy(std::vector<GenieChatMessage> &user_message_vector,
                                                     const std::string &prompt_system,
                                                     const std::string &prompt_start,
                                                     int contextSize)
{
    std::vector<std::string> messages;
    std::string format_content;
    auto handle = model_config_.get_genie_model_handle().lock();
    size_t string_length = handle->TokenLength(prompt_system) + handle->TokenLength(prompt_start);

    if (string_length <= contextSize)
    {
        messages.push_back(prompt_system);
        messages.push_back(prompt_start);
    }

    size_t vector_length = user_message_vector.size();
    auto &j = model_config_.get_prompt_template();
    for (size_t i = 0; i < vector_length; i++)
    {
        std::string role = user_message_vector[i].role;
        std::string content = user_message_vector[i].content;
        if (role == "tool")
        {
            format_content = str_replace(j["tool"], "string", content);
        }
        else if (role == "user")
        {
            format_content = str_replace(j["user"], "string", content);
        }
        else if (role == "assistant")
        {
            format_content = str_replace(j["assistant"], "string", content);
        }
        string_length += handle->TokenLength(format_content);
        if (string_length <= contextSize)
        {
            messages.insert(messages.begin() + 1, format_content);
        }
        else
        {
            My_Log{} << "Message is too long, skipping: " << std::endl;
            My_Log{} << "Total message number: " << vector_length << std::endl;
            My_Log{} << "Current message number: " << i + 1 << std::endl;

            if (i == 0)
            {
                My_Log{} << "The first message is too long. Return empty string." << std::endl;
                return "";
            }
            break;
        }
    }

    return std::accumulate(messages.begin(), messages.end(), std::string{});
}

ChatHistory::ChatHistory(IModelConfig &model_config)
{ impl_ = new Impl{model_config}; }

ChatHistory::~ChatHistory()
{
    if (impl_)
        delete impl_;
}

std::string ChatHistory::BuildPrompt(json &data, bool &is_tool)
{
    is_tool = false;
    auto &model_config = impl_->model_config_;
    if (model_config.get_query_type().v_ != QueryType::TextQuery)
    {
        for (const auto &e: data["messages"])
        {
            if (e["role"] == "user")
            {
                return e["content"];
            }
        }
        return "{error}";
    }

    // parse data
    json msg = data["messages"];
    json tools = data["tools"];
    std::string userToolsPrompt;   // Tool description sent by client
    std::string systemDefaultPrompt = "You are a helpful assistant.";
    std::string startDefaultPrompt;
    std::string userContent;               // question sent by client
    std::string userToolCallBackResult;    // Client tool call result
    auto handle = model_config.get_genie_model_handle().lock();
    auto &context_size = model_config.context_size();

    // add tool description
    if (tools != nullptr)
    {
        is_tool = true;
        Clear();
        // My_Log{} << "Tools model, clear chat history." << msg << std::endl;

        size_t toolsLength = 0;
        for (const auto &element: tools)
        {
            std::string userToolPrompt = json_to_str(element);
            size_t tool_length = handle->TokenLength(userToolPrompt);
            if (toolsLength + tool_length < context_size - model_config.getminOutputNum())
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
        userToolsPrompt = str_replace(Impl::tool_prompt_template, "{tool_descs}", userToolsPrompt);
    }

    // Extract the content sent by the client, including the user's question, system prompts, and tool invocation results.
    for (const auto &element: msg)
    { // TODO: Handle history message.
        auto role = get_json_value(element, "role", Impl::BLANK_STRING);
        if (role == "user")
        {
            userContent = get_json_value(element, "content", Impl::BLANK_STRING);
            impl_->add_message("user", userContent);
        }
        else if (role == "system")
        {
            systemDefaultPrompt = get_json_value(element, "content", Impl::BLANK_STRING);
            systemDefaultPrompt = str_replace(systemDefaultPrompt, "\\n", "\n");
        }
        else if (role == "tool")
        {  // TODO: Check tool id.
            userToolCallBackResult += trim_empty_lines(get_json_value(element, "content", Impl::BLANK_STRING));
            userToolCallBackResult = "<tool_response>\n" + userToolCallBackResult + "\n</tool_response>\n";
            impl_->add_message("tool", userToolCallBackResult);
        }
    }

    // build system prompt
    systemDefaultPrompt += userToolsPrompt;

    // If it is a thinkable model, change the system prompt.
    if (is_thinking_model(model_config.getModelName()))
    {
        if (model_config.getenableThinking())
        {
            systemDefaultPrompt += "/think";
        }
        else
        {
            systemDefaultPrompt += "/no_think";
            startDefaultPrompt += Impl::FILL_THINK;
        }
    }

    // build model input
    int inputTokenNum = std::max(context_size - model_config.getminOutputNum(), context_size / 2);


    auto &j = model_config.get_prompt_template();
    /* @formatter:off */
    std::string modelInputContent = impl_->get_user_message(model_config.getnumResponse(),
                                                            str_replace(j["system"], "string", systemDefaultPrompt),
                                                            j["start"].get<std::string>() + startDefaultPrompt,
                                                            inputTokenNum);
    /* @formatter:on */
    return modelInputContent;
}

void ChatHistory::AddMessage(const std::string &role, const std::string &content)
{
    impl_->add_message(role, content);
}

bool ChatHistory::import_from_json(const nlohmann::json &j)
{
    try
    {
        if (!j.contains("history") || !j["history"].is_array())
        {
            return false; // if missing history field or format error, return false
        }

        std::vector<Impl::GenieChatMessage> new_history;
        for (const auto &item: j["history"])
        {
            if (!item.contains("role") || !item.contains("content") ||
                !item["role"].is_string() || !item["content"].is_string())
            {
                return false; // Each message must have the role and content fields, and they must be strings.
            }

            Impl::GenieChatMessage msg;
            msg.role = item["role"];
            msg.content = item["content"];

            // Verify whether the role field is valid.
            if (msg.role != "user" && msg.role != "assistant" && msg.role != "tool")
            {
                return false;
            }

            new_history.push_back(msg);
        }

        // Successfully parsed and replaced the current history.
        impl_->history = std::move(new_history);
        return true;

    }
    catch (const std::exception &e)
    {
        My_Log{} << "Import failed: " << e.what();
        return false;
    }
}

nlohmann::json ChatHistory::export_to_json() const
{
    nlohmann::json j;
    j["history"] = nlohmann::json::array();
    for (const auto &msg: impl_->history)
    {
        j["history"].push_back({
                                       {"role",    msg.role},
                                       {"content", msg.content}
                               });
    }
    return j;
}

void ChatHistory::Print() const
{
    for (const auto &msg: impl_->history)
    {
        My_Log{} << "[" << msg.role << "]: " << msg.content << std::endl;
    }
}

void ChatHistory::Limit(size_t max_size)
{
    auto &history = impl_->history;
    if (history.size() > max_size)
    {
        history.erase(history.begin(), history.end() - max_size);
    }
}

void ChatHistory::Clear()
{ impl_->history.clear(); }
