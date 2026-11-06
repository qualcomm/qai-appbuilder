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
#include "../model/model_config.h"
#include "../model/model_instance_config.h"
#include <set>

class ChatHistory::Impl
{
public:
    explicit Impl(const json &prompt_template)
        : prompt_template_(prompt_template)
    {}

    const std::vector<GenieChatMessage> &get() const
    { return history; }

    std::string GetUserMessage(const std::string &prompt_system,
                               const std::string &prompt_start,
                               const std::string &assistant_turn_prefix,
                               const std::string &trailing_note)
    {
        std::vector<std::string> messages;
        const auto &j = prompt_template_;

        bool has_valid_template = j.is_object()
                                  && j.contains("user")    && j["user"].is_string()
                                  && j.contains("assistant") && j["assistant"].is_string()
                                  && j.contains("tool")    && j["tool"].is_string();
        if (!has_valid_template) {
            My_Log{My_Log::Level::kError}
                << "[ChatHistory::GetUserMessage] prompt_template is invalid (type=" << j.type_name()
                << "). This model's prompt.json may not have been loaded correctly. "
                << "Check [LoadModel] logs for 'Error loading prompt file' or 'file.good()=false'." << std::endl;
            throw std::runtime_error(std::string{"prompt_template is not a valid JSON object (type="}
                                     + j.type_name() + "). "
                                     + "Ensure the model directory contains a valid prompt.json file.");
        }
        messages.push_back(prompt_system);
        messages.push_back(prompt_start);

        size_t count = history.size();
        My_Log{} << "History size: " << count << ", returning all messages in history" << std::endl;

        if (count == 0)
        {
            if (!trailing_note.empty())
                messages.insert(messages.end() - 1, trailing_note);
            return std::accumulate(messages.begin(), messages.end(), std::string{});
        }

        const std::string assistant_tmpl = j["assistant"].get<std::string>();
        const size_t slot = assistant_tmpl.find("string");
        const std::string assistant_tail = slot == std::string::npos ? "" : assistant_tmpl.substr(slot + 6);

        for (size_t i = 0; i < count; i++)
        {
            auto &msg = history[i];

            std::string content = msg.content;

            std::string format_content;
            if (msg.role == "tool")
            {
                format_content = str_replace(j["tool"], "string", content);
            }
            else if (msg.role == "user")
            {
                format_content = str_replace(j["user"], "string", content);
            }
            else if (msg.role == "assistant" && !assistant_turn_prefix.empty() && slot != std::string::npos)
            {
                format_content = assistant_turn_prefix + content + assistant_tail;
            }
            else if (msg.role == "assistant")
            {
                format_content = str_replace(j["assistant"], "string", content);
            }

            messages.insert(messages.end() - 1, format_content);
        }

        if (!trailing_note.empty())
            messages.insert(messages.end() - 1, trailing_note);

        return std::accumulate(messages.begin(), messages.end(), std::string{});
    }

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

    const json &prompt_template_;

    std::vector<GenieChatMessage> history;
};

ChatHistory::ChatHistory(ModelInstanceConfig &model_config)
{
    impl_ = std::make_unique<Impl>(model_config.get_prompt_template());
}

ChatHistory::ChatHistory(IModelConfig &model_config)
{
    impl_ = std::make_unique<Impl>(model_config.get_prompt_template());
}

ChatHistory::~ChatHistory() = default;

void ChatHistory::AddMessage(const std::string &role, const std::string &content)
{ impl_->add_message(role, content); }

void ChatHistory::PrependToLastMessage(const std::string &prefix)
{
    if (impl_->history.empty()) return;
    impl_->history.back().content = prefix + impl_->history.back().content;
}

bool ChatHistory::IsEmpty() const
{ return impl_->history.empty(); }

bool ChatHistory::import_from_json(const json &j)
{
    try
    {
        if (!j.contains("history") || !j["history"].is_array())
        {
            return false;
        }

        std::vector<GenieChatMessage> new_history;
        for (const auto &item: j["history"])
        {
            if (!item.contains("role") || !item.contains("content") ||
                !item["role"].is_string() || !item["content"].is_string())
            {
                return false;
            }

            GenieChatMessage msg;
            msg.role = item["role"];
            msg.content = item["content"];

            if (msg.role != "user" && msg.role != "assistant" && msg.role != "tool")
            {
                return false;
            }

            new_history.push_back(msg);
        }

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
        std::string content_safe = msg.content;
        std::replace(content_safe.begin(), content_safe.end(), '\r', ' ');
        std::replace(content_safe.begin(), content_safe.end(), '\n', ' ');
        My_Log{} << "[" << msg.role << "]: " << content_safe << std::endl;
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

std::string ChatHistory::GetUserMessage(const std::string &prompt_system, const std::string &prompt_start,
                                        const std::string &assistant_turn_prefix, const std::string &trailing_note)
{ return impl_->GetUserMessage(prompt_system, prompt_start, assistant_turn_prefix, trailing_note); }
