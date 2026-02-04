//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "chat_history.h"
#include "log.h"
#include "utils.h"
#include "../context/context_base.h"

class ChatHistory::Impl
{
public:
    struct GenieChatMessage
    {
        std::string role; // role："user", "assistant", "tool"
        std::string content; // message content
    };

    explicit Impl(IModelConfig &model_config) : model_config_{model_config}
    {}


    /*
    "Here is a sample:\n";
    "<tool_call>\n"
    "{\"name\": \"example_function\", \"arguments\": {\"arg1\": \"value1\", \"arg2\": \"value2\"}}\n"
    "</tool_call>\n";
    */
    const std::vector<GenieChatMessage> &get() const
    {
        return history;
    }

    std::string GetUserMessage(const std::string &prompt_system,
                               const std::string &prompt_start)
    {
        auto num_response = model_config_.getnumResponse();
        int contextSize = std::max(model_config_.context_size() - model_config_.getminOutputNum(),
                                   model_config_.context_size() / 2);
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

        return data_process_strategy(user_message_vector, prompt_system, prompt_start, contextSize);
    }

    std::string data_process_strategy(std::vector<GenieChatMessage> &user_message_vector,
                                      const std::string &prompt_system,
                                      const std::string &prompt_start,
                                      int contextSize);

    void add_message(const std::string &role, const std::string &content)
    {
        history.emplace_back(GenieChatMessage{role, content});
    }

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
        else
            if (role == "user")
            {
                format_content = str_replace(j["user"], "string", content);
            }
            else
                if (role == "assistant")
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
{
    impl_ = new Impl{model_config};
}

ChatHistory::~ChatHistory()
{
    if (impl_)
        delete impl_;
}

void ChatHistory::AddMessage(const std::string &role, const std::string &content)
{
    impl_->add_message(role, content);
}

bool ChatHistory::import_from_json(const json &j)
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

json ChatHistory::export_to_json() const
{
    nlohmann::json j;
    j["history"] = nlohmann::json::array();
    for (const auto &msg: impl_->history)
    {
        j["history"].push_back({
            {"role", msg.role},
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
{
    impl_->history.clear();
}

std::string ChatHistory::GetUserMessage(const std::string &prompt_system, const std::string &prompt_start)
{
    return impl_->GetUserMessage(prompt_system, prompt_start);
}
