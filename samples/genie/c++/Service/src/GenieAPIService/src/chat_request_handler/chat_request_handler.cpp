#include "chat_request_handler.h"
#include "utils.h"
#include "log.h"

#include "../chat_history/chat_history.h"
#include "../context/context_base.h"

const int DOCS_MAX_SIZE = CONTEXT_SIZE - 1024;
const int DOCS_MAX_QUERY_TIMES = 3;

void ChatRequestHandler::FetchModelList(const httplib::Request &req, httplib::Response &res)
{
    json models = model_manager.get_model_list();
    res.set_content(models.dump(2), MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::ContextSize(const httplib::Request &req, httplib::Response &res)
{
    json contextSize;
    contextSize["contextsize"] = model_manager.context_size();
    res.set_content(contextSize.dump(2), MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::ModelStop(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    std::string text = data.value("text", "");
    if (text == "stop")
    {
        auto handle = model_manager.get_genie_model_handle().lock();
        handle->Stop();
    }
    res.set_content("", MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::ClearMessage(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    std::string text = data.value("text", "");
    if (text == "Clear")
    {
        chatHistory->Clear();
    }
    My_Log{} << RED << "history message have been delete!" << RESET << std::endl;
    res.set_content("", MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::ReloadMessage(const httplib::Request &req, httplib::Response &res)
{
    auto j = json::parse(req.body, nullptr, false);
    if (chatHistory->import_from_json(j))
    {
        res.set_content("{\"status\": \"success\"}", "application/json");
    }
    else
    {
        res.status = 400;
        res.set_content("{\"error\": \"Invalid history format\"}", "application/json");
    }
}

void ChatRequestHandler::FetchMessage(const httplib::Request &req, httplib::Response &res)
{
    res.status = 200;
    res.set_content(json_to_str(chatHistory->export_to_json()), MIMETYPE_JSON);
}

void ChatRequestHandler::TextSplitter(const httplib::Request &req, httplib::Response &res)
{
    // When writing the C++ implementation of RecursiveCharacterTextSplitter, I referenced the Python code in LangChain.
    // https://github.com/langchain-ai/langchain/blob/master/libs/text-splitters/langchain_text_splitters/character.py#L58
    class RecursiveCharacterTextSplitter
    {
    private:
        std::vector<std::string> _separators;
        bool _keep_separator;
        size_t _chunk_size;
        std::function<size_t(const std::string &)> _length_function;

        std::vector<std::string> _merge_splits(const std::vector<std::string> &splits, const std::string &separator)
        {
            std::vector<std::string> docs;
            std::string current_doc;
            int total = 0;

            for (const auto &split: splits)
            {
                size_t len = _length_function(split);
                if (total + len > _chunk_size)
                {
                    if (!current_doc.empty())
                    {
                        docs.push_back(current_doc);
                        current_doc.clear();
                    }
                    total = 0;
                }
                if (!current_doc.empty()) current_doc += separator;
                current_doc += split;
                total += len;
            }
            if (!current_doc.empty()) docs.push_back(current_doc);

            return docs;
        }

        std::vector<std::string> _split_text(const std::string &text, const std::vector<std::string> &separators)
        {
            if (separators.empty()) return {text};

            std::string separator = separators.front();
            std::vector<std::string> splits;
            std::regex re(std::regex_replace(separator, std::regex(R"([\.\^\$\*\+\-\?\(\)\[\]\{\}\|\\])"), R"(\$&)"));
            std::sregex_token_iterator iter(text.begin(), text.end(), re, _keep_separator ? -1 : 0);
            std::sregex_token_iterator end;

            for (; iter != end; ++iter)
            {
                splits.push_back(*iter);
            }

            std::vector<std::string> final_chunks;
            for (const auto &split: splits)
            {
                if (_length_function(split) < _chunk_size)
                {
                    final_chunks.push_back(split);
                }
                else
                {
                    auto deeper_chunks = _split_text(split, {separators.begin() + 1, separators.end()});
                    final_chunks.insert(final_chunks.end(), deeper_chunks.begin(), deeper_chunks.end());
                }
            }

            return _merge_splits(final_chunks, _keep_separator ? separator : "");
        }

    public:
        RecursiveCharacterTextSplitter(
                const std::vector<std::string> &separators = {"\n\n", "\n", " ", ""},
                bool keep_separator = true,
                int chunk_size = DOCS_MAX_SIZE,
                std::function<size_t(const std::string &)> length_function = [](const std::string &s)
                { return static_cast<size_t>(s.length()); }
        ) : _separators(separators), _keep_separator(keep_separator),
            _chunk_size(chunk_size), _length_function(length_function)
        {}

        std::vector<std::string> split_text(const std::string &text)
        {
            return _split_text(text, _separators);
        }
    };

    json data = json::parse(req.body, nullptr, false);
    if (!data.is_object())
    {
        res.status = 400;
        res.set_content(R"({"error": "Invalid JSON."})", MIMETYPE_JSON);
        return;
    }

    std::string text = data.value("text", "");
    int maxLength = data.value("max_length", 0);
    if (maxLength <= 0)
    {
        maxLength = model_manager.context_size() - model_manager.getminOutputNum();
    }

    std::vector<std::string> separators = data.value("separators", std::vector<std::string>{});


    auto handle = model_manager.get_genie_model_handle().lock();
    auto lengthFn = [&handle](const std::string &s)
    {
        return handle->TokenLength(s);
    };

    static const std::vector<std::string> &SEPARATORS = {"\n\n", "\n", "。", "！", "？", "，", ".", "?", "!", ",", " ", ""};
    if (separators.empty())
    {
        separators = SEPARATORS;
    }
    RecursiveCharacterTextSplitter splitter(separators, true, maxLength, lengthFn);
    auto chunks = splitter.split_text(text);

    json jsonData;
    std::vector<json> content;

    for (const auto &item: chunks)
    {
        json item_json;
        item_json["text"] = item;
        item_json["length"] = handle->TokenLength(item);
        content.push_back(item_json);
    }
    jsonData["content"] = content;
    jsonData["object"] = "list";
    res.set_content(jsonData.dump(2), MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::ChatCompletions(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    if (!data.is_object())
    {
        res.status = 400;
        res.set_content(R"({"error": "Invalid JSON."})", MIMETYPE_JSON);
        return;
    }

    std::string modelName = data.value("model", "");
    bool new_model;
    if (!model_manager.LoadModelByName(modelName, new_model))
    {
        res.status = 500;
        res.set_content(R"({"error": "Model load failed."})", MIMETYPE_JSON);
        return;
    }

    if (new_model)
        dispatcherPtr_->ResetProcessor();

    auto handle = model_manager.get_genie_model_handle().lock();
    if (!handle)
    {
        res.status = 500;
        res.set_content(R"({"error": "Model context unavailable."})", MIMETYPE_JSON);
        return;
    }

    if (modelName.find("lora") != std::string::npos)
    {
        My_Log{} << "Load lora" << std::endl;
        std::unordered_map<std::string, float> loraAlphaValue{};
        loraAlphaValue["lora_alpha"] = model_manager.getloraAlpha();
        std::string engineRole{"primary"};
        handle->applyLora(engineRole, model_manager.getloraAdapter());
        handle->setLoraStrength(engineRole, loraAlphaValue);
    }

    auto size = std::to_string(get_json_value(data, "size", model_manager.context_size()));
    auto temp = std::to_string(get_json_value(data, "temp", 0.8));
    auto top_k = std::to_string(get_json_value(data, "top_k", 40));
    auto top_p = std::to_string(get_json_value(data, "top_p", 0.95));
    handle->SetParams(size, temp, top_k, top_p);

    if (!dispatcherPtr_->Prepare(data, req))
    {
        res.status = 500;
        res.set_content(R"({"error": "prompt is not correct."})", MIMETYPE_JSON);
        return;
    }

    get_json_value(data, "stream", false) ?
    res.set_chunked_content_provider(
            "text/event-stream",
            [this](size_t offset, httplib::DataSink &sink)
            { return dispatcherPtr_->sendStreamResponse(offset, sink); },
            nullptr
    ) :
    dispatcherPtr_->sendNormalResponse(res);
}

void ChatRequestHandler::FetchProfile(const httplib::Request &req, httplib::Response &res)
{
    auto handle = model_manager.get_genie_model_handle().lock();
    json result = handle->HandleProfile();
    if (!result.empty())
    {
        res.set_content(json_to_str(result), MIMETYPE_JSON);
        res.status = 200;
    }
}

void ChatRequestHandler::ImageGenerate(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    res.set_content("", MIMETYPE_JSON);
    res.status = 501;
}

void ChatRequestHandler::HandleWelcome(const httplib::Request &req, httplib::Response &res)
{
    static const auto root_html = R"(
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="UTF-8">
    <title>Genie API Service</title>
    <style>
    body { word-wrap: break-word; white-space: normal; }
    h1 {text-align: center;}
    </style>
    </head>
    <body>
    <br><br>
    <h1>Genie API Service IS Running.</h1>
    </body>
    </html>
    )";
    res.set_content(root_html, "text/html");
}

void ChatRequestHandler::FetchModelStatus(const Request &req, Response &res)
{
    json result;
    result["loading"] = std::to_string(!model_manager.IsLoaded());
    res.set_content(result, MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::UnloadModel(const Request &req, Response &res)
{
    model_manager.UnloadModel();
    res.set_content("", MIMETYPE_JSON);
    res.status = 200;
}

