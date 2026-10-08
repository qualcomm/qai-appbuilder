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
                               const std::string &prompt_start)
    {
        std::vector<std::string> messages;
        const auto &j = prompt_template_;

        // 修复：检查 prompt_template 是否有效（非 null 且为 object 类型）
        // 若 prompt.json 读取失败（file.good()=false、格式错误等），
        // prompt_template 会保持 null，对 null 类型的 const json 使用字符串键会抛出 type_error.305。
        // 此处提前检查，避免在 str_replace(j["tool"], ...) 等处触发异常。
        bool has_valid_template = j.is_object()
                                  && j.contains("user")    && j["user"].is_string()
                                  && j.contains("assistant") && j["assistant"].is_string()
                                  && j.contains("tool")    && j["tool"].is_string();
        if (!has_valid_template) {
            My_Log{My_Log::Level::kError}
                << "[ChatHistory::GetUserMessage] prompt_template is invalid (type=" << j.type_name()
                << "). This model's prompt.json may not have been loaded correctly. "
                << "Check [LoadModel] logs for 'Error loading prompt file' or 'file.good()=false'." << std::endl;
            // 抛出异常，让上层 BuildPrompt 的 catch 捕获并返回明确错误
            throw std::runtime_error(std::string{"prompt_template is not a valid JSON object (type="}
                                     + j.type_name() + "). "
                                     + "Ensure the model directory contains a valid prompt.json file.");
        }
        messages.push_back(prompt_system);
        messages.push_back(prompt_start);

        size_t count = history.size();
        My_Log{} << "History size: " << count << ", returning all messages (no compression)" << std::endl;

        if (count == 0)
        {
            return std::accumulate(messages.begin(), messages.end(), std::string{});
        }

        // 直接处理所有消息，不进行压缩或丢弃
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
            else if (msg.role == "assistant")
            {
                format_content = str_replace(j["assistant"], "string", content);
            }

            // 插入到 start prompt 之前
            messages.insert(messages.end() - 1, format_content);
        }

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

bool ChatHistory::import_from_json(const json &j)
{
    try
    {
        if (!j.contains("history") || !j["history"].is_array())
        {
            return false; // if missing history field or format error, return false
        }

        std::vector<GenieChatMessage> new_history;
        for (const auto &item: j["history"])
        {
            if (!item.contains("role") || !item.contains("content") ||
                !item["role"].is_string() || !item["content"].is_string())
            {
                return false; // Each message must have the role and content fields, and they must be strings.
            }

            GenieChatMessage msg;
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
        // 过滤 \r 和 \n，防止控制字符破坏日志格式（例如 tool 消息含 Windows CRLF）
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

std::string ChatHistory::GetUserMessage(const std::string &prompt_system, const std::string &prompt_start)
{ return impl_->GetUserMessage(prompt_system, prompt_start); }
