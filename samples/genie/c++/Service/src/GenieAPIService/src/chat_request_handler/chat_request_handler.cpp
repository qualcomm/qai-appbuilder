//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "chat_request_handler.h"
#include "utils.h"
#include "log.h"
#include "model_input_builder.h"
#include <ctime>
#include <set>
#include <algorithm>
#include <filesystem>
#include <thread>
#include <chrono>
#include "text_splitter.h"
#include "../GenieAPIService.h"
#include "../model/model_manager.h"
#include "../response/response_dispatcher.h"
#include "../response/response_tools.h"
#include "../gateway/gateway/gateway.h"
#include "request_trace_writer.h"
#include "message_pre_filter.h"
#include "task_memo_store.h"
#include "summary_cache.h"
#include "tool_call_circuit_breaker_store.h"
#include "tool_call_repetition_store.h"

namespace fs = std::filesystem;

namespace
{
json SamplerFallback(const ModelInstanceConfig &config, const std::string &key)
{
    auto &sampler = config.sampler();
    return (sampler.contains(key) && sampler.at(key).is_number()) ? sampler.at(key) : json();
}

constexpr float kSpeculativeToolCallTemperature = 0.1f;
constexpr float kToolCallTemperature = 0.7f;

float ToolCallTemperatureCap(const ModelInstanceConfig &config)
{
    return config.is_speculative_draft_configured() ? kSpeculativeToolCallTemperature : kToolCallTemperature;
}

void ApplyOutputSizeBudget(ContextBase &handle, ModelInstanceConfig &config,
                          size_t prompt_tokens, const json &temperature)
{
    int context_size = config.get_context_size();
    int available_output = context_size - static_cast<int>(prompt_tokens);
    int min_output = config.getminOutputNum();
    available_output = std::max(available_output, min_output);
    available_output = std::min(available_output, context_size);
    My_Log{My_Log::Level::kInfo}
        << "Prompt tokens: " << prompt_tokens
        << ", Context size: " << context_size
        << ", Max output tokens: " << available_output << std::endl;

    json params{{"size", available_output}};
    if (temperature.is_number())
        params["temp"] = temperature;
    json top_k = SamplerFallback(config, "top-k");
    if (top_k.is_number())
        params["top_k"] = top_k;
    json top_p = SamplerFallback(config, "top-p");
    if (top_p.is_number())
        params["top_p"] = top_p;
    handle.SetParamsByConfig(params);
}

std::shared_ptr<LoadedModel> ResolveLoadedModel(ModelManager &manager, const std::string &model_name,
                                                httplib::Response &res)
{
    if (model_name.empty())
    {
        res.status = 400;
        res.set_content(R"({"error": "model is required"})", ResponseDispatcher::MIMETYPE_JSON);
        return nullptr;
    }
    auto loaded = manager.GetModel(model_name);
    if (!loaded || !loaded->is_loaded || !loaded->config || !loaded->context)
    {
        json err = {{"error", "model '" + model_name + "' is not loaded"}};
        res.status = 404;
        res.set_content(err.dump(), ResponseDispatcher::MIMETYPE_JSON);
        return nullptr;
    }
    return loaded;
}

std::vector<std::shared_ptr<LoadedModel>> ResolveQueriedModels(ModelManager &manager, const std::string &model_name,
                                                                httplib::Response &res)
{
    std::vector<std::shared_ptr<LoadedModel>> result;
    if (!model_name.empty())
    {
        auto loaded = ResolveLoadedModel(manager, model_name, res);
        if (loaded)
        {
            result.push_back(std::move(loaded));
        }
        return result;
    }
    for (auto &loaded: manager.GetAllLoadedModels())
    {
        if (loaded && loaded->is_loaded && loaded->config && loaded->context)
        {
            result.push_back(std::move(loaded));
        }
    }
    return result;
}

std::string ModelNameFromBody(const std::string &body)
{
    json data = json::parse(body, nullptr, false);
    if (!data.is_object() || !data.contains("model") || !data["model"].is_string())
    {
        return "";
    }
    return data["model"].get<std::string>();
}

void WriteRequestTrace(const std::string &request_id, const std::string &model_name, bool is_stream,
                       const ResponseDispatcher &dispatcher, const std::shared_ptr<ContextBase> &handle,
                       const PromptLedger &ledger, const std::string &request_body)
{
    auto &writer = RequestTraceWriter::GetInstance();
    if (!writer.IsEnabled())
    {
        return;
    }
    RequestTraceWriter::Record rec;
    rec.request_id = request_id;
    rec.model_name = model_name;
    rec.is_stream = is_stream;
    rec.final_prompt = dispatcher.GetPromptText();
    rec.raw_output = dispatcher.GetRawOutput();
    rec.finish_reason = dispatcher.GetFinishReason();
    if (!dispatcher.GetToolResponse().empty())
    {
        try
        {
            rec.tool_calls = ResponseTools::format_tool_calls(dispatcher.GetToolResponse());
        }
        catch (const std::exception &)
        {
            rec.tool_calls = dispatcher.GetToolResponse();
        }
    }
    rec.backend_metrics = handle ? handle->GetLastQueryTraceMetrics() : json(nullptr);
    rec.prompt_ledger = ledger.ToJson();
    rec.request_body = request_body;
    if (handle)
    {
        rec.token_length = [handle](const std::string &s) { return handle->TokenLength(s); };
    }
    writer.WriteRecord(rec);
}
}

ChatRequestHandler::ChatRequestHandler(GenieService *srv) :
        model_manager(*srv->modelManager),
        srv_{srv}
{
    const auto &routing_config = model_manager.GetRoutingConfig();
    const auto &cloud_config = model_manager.GetCloudModelConfig();
    const auto &enterprise_cloud_config = model_manager.GetEnterpriseCloudModelConfig();

    if (routing_config.enabled)
    {
        genieRoutingGateway_ = std::make_shared<GenieRoutingGateway>(
            model_manager, routing_config, cloud_config, enterprise_cloud_config);
        My_Log{} << "[ChatRequestHandler] GenieRoutingGateway initialized, routing.enabled="
                 << routing_config.enabled
                 << ", cloud.enabled=" << cloud_config.enabled
                 << ", enterprise_cloud.enabled=" << enterprise_cloud_config.enabled << std::endl;
    }
    else
    {
        My_Log{} << "[ChatRequestHandler] GenieRoutingGateway not initialized (routing.enabled=false)" << std::endl;
    }
}

void ChatRequestHandler::FetchModelList(const httplib::Request &req, httplib::Response &res)
{
    json models;
    std::vector<json> model_list;
    const long long now_ts = static_cast<long long>(std::time(nullptr));

    auto to_lower = [](const std::string &s) -> std::string {
        std::string r = s;
        std::transform(r.begin(), r.end(), r.begin(),
                       [](unsigned char c) { return static_cast<unsigned char>(std::tolower(c)); });
        return r;
    };

    auto loaded_models = model_manager.GetAllLoadedModels();
    std::vector<bool> loaded_matched(loaded_models.size(), false);

    std::vector<json> disk_models = model_manager.ScanModelDirectory();

    for (auto &m : disk_models)
    {
        m["object"]   = "model";
        m["created"]  = now_ts;
        m["owned_by"] = "owner";
        const std::string disk_id_lower = to_lower(m["id"].get<std::string>());
        m["is_loaded"] = false;
        for (size_t i = 0; i < loaded_models.size(); ++i)
        {
            const auto &loaded = loaded_models[i];
            if (!loaded || !loaded->config)
                continue;
            std::string name_lower = to_lower(loaded->config->get_model_name());
            fs::path p(loaded->config->get_model_path());
            std::string dir_lower = to_lower(p.filename().generic_string());
            bool is_match = (!name_lower.empty() && disk_id_lower == name_lower) ||
                            (!dir_lower.empty() && disk_id_lower == dir_lower);
            if (is_match)
            {
                loaded_matched[i] = true;
                m["is_loaded"]      = true;
                m["context_length"] = loaded->config->get_context_size();
                m["backend"]        = loaded->backend;
                m["device"]         = loaded->device;
                break;
            }
        }
        model_list.push_back(m);
    }

    for (size_t i = 0; i < loaded_models.size(); ++i)
    {
        if (loaded_matched[i])
            continue;
        const auto &loaded = loaded_models[i];
        if (!loaded || !loaded->config)
            continue;
        json m;
        m["id"]             = loaded->config->get_model_name();
        m["object"]         = "model";
        m["created"]        = now_ts;
        m["owned_by"]       = "owner";
        m["is_loaded"]      = true;
        m["context_length"] = loaded->config->get_context_size();
        m["backend"]        = loaded->backend;
        m["device"]         = loaded->device;
        model_list.push_back(m);
    }

    models["data"]   = model_list;
    models["object"] = "list";
    res.set_content(models.dump(2), ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::ContextSize(const httplib::Request &req, httplib::Response &res)
{
    std::string model_name = req.get_param_value("model");
    if (model_name.empty() && req.method == "POST")
    {
        model_name = ModelNameFromBody(req.body);
    }
    const auto queried = ResolveQueriedModels(model_manager, model_name, res);
    if (!model_name.empty() && queried.empty())
    {
        return;
    }

    json entries = json::array();
    for (const auto &loaded: queried)
    {
        entries.push_back({{"model", loaded->config->get_model_name()},
                           {"contextsize", loaded->config->get_context_size()}});
    }
    json result = {{"models", std::move(entries)}, {"loaded_models", queried.size()}};
    res.set_content(result.dump(2), ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::ServiceExit(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    std::string text = data.value("text", "");
    res.set_content(R"({"status":"stopped"})", ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
    res.set_header("Connection", "close");
    if (text == "stop")
    {
        auto srv = srv_;
        std::thread([srv]() {
            std::this_thread::sleep_for(std::chrono::seconds(2));
            srv->ServiceStop();
        }).detach();
    }
}

void ChatRequestHandler::ModelStop(const httplib::Request &req, httplib::Response &res)
{
    const std::string model_name = ModelNameFromBody(req.body);
    auto loaded = ResolveLoadedModel(model_manager, model_name, res);
    if (!loaded)
    {
        return;
    }

    json data = json::parse(req.body, nullptr, false);
    if (data.value("text", "") != "stop")
    {
        res.status = 400;
        res.set_content(R"({"error": "text must be 'stop'"})", ResponseDispatcher::MIMETYPE_JSON);
        return;
    }
    loaded->context->Stop();
    json ok = {{"status", "stopped"}, {"model", loaded->config->get_model_name()}};
    res.set_content(ok.dump(), ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::ClearMessage(const httplib::Request &req, httplib::Response &res)
{
    const std::string model_name = ModelNameFromBody(req.body);
    auto loaded = ResolveLoadedModel(model_manager, model_name, res);
    if (!loaded)
    {
        return;
    }
    loaded->context->ClearConversationState();

    ClearPreFilterSessionStates();
    TaskMemoStore::GetInstance().Clear();
    SummaryCache::GetInstance().Clear();
    ToolCallCircuitBreakerStore::GetInstance().Clear();
    ToolCallRepetitionStore::GetInstance().Clear();

    res.set_content(R"({"status":"cleared"})", ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::ReloadMessage(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    if (!data.is_object())
    {
        res.status = 400;
        res.set_content(R"({"error": "Invalid JSON."})", ResponseDispatcher::MIMETYPE_JSON);
        return;
    }

    std::string action = data.value("action", "");
    if (action == "import_history")
    {
        json history_arr = data.value("history", json::array());
        if (!history_arr.is_array())
        {
            res.status = 400;
            res.set_content(R"({"error": "history must be an array"})", ResponseDispatcher::MIMETYPE_JSON);
            return;
        }
        for (const auto &item : history_arr)
        {
            if (!item.is_object() || !item.contains("role") || !item.contains("content")
                || !item["role"].is_string() || !item["content"].is_string())
            {
                res.status = 400;
                res.set_content(R"({"error": "Each history item must have string role and content"})", ResponseDispatcher::MIMETYPE_JSON);
                return;
            }
            std::string role = item["role"];
            if (role != "user" && role != "assistant" && role != "tool")
            {
                res.status = 400;
                res.set_content(R"({"error": "Invalid role in history"})", ResponseDispatcher::MIMETYPE_JSON);
                return;
            }
        }
        res.status = 200;
        res.set_content(R"({"status": "ok"})", ResponseDispatcher::MIMETYPE_JSON);
        return;
    }

    res.status = 400;
    res.set_content(R"({"error": "Unknown or missing action"})", ResponseDispatcher::MIMETYPE_JSON);
}

void ChatRequestHandler::FetchMessage(const httplib::Request &req, httplib::Response &res)
{
    res.status = 200;
    res.set_content("{\"history\": []}", ResponseDispatcher::MIMETYPE_JSON);
}

void ChatRequestHandler::TextSplitter(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    if (!data.is_object())
    {
        res.status = 400;
        res.set_content(R"({"error": "Invalid JSON."})", ResponseDispatcher::MIMETYPE_JSON);
        return;
    }

    std::string text = data.value("text", "");
    std::string model_name = data.contains("model") && data["model"].is_string() ? data["model"].get<std::string>() : "";

    auto loaded = ResolveLoadedModel(model_manager, model_name, res);
    if (!loaded)
    {
        return;
    }
    auto& config = *loaded->config;

    int maxLength = data.value("max_length", 0);
    if (maxLength <= 0)
    {
        maxLength = config.get_context_size() - config.getminOutputNum();
    }

    std::vector<std::string> separators = data.value("separators", std::vector<std::string>{});
    auto handle = loaded->context;
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
    res.set_content(jsonData.dump(2), ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::ChatCompletions(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    if (!data.is_object())
    {
        res.status = 400;
        res.set_content(R"({"error": "Invalid JSON."})", ResponseDispatcher::MIMETYPE_JSON);
        return;
    }

    std::string modelName = data.value("model", "");

    static const std::string kLocalPrefix = "local::";
    if (modelName.size() > kLocalPrefix.size() &&
        modelName.substr(0, kLocalPrefix.size()) == kLocalPrefix)
    {
        modelName = modelName.substr(kLocalPrefix.size());
    }

    if (modelName.empty())
    {
        res.status = 400;
        res.set_content(R"({"error": "model is required"})", ResponseDispatcher::MIMETYPE_JSON);
        return;
    }

    auto loaded_model = model_manager.GetModel(modelName);

    bool model_confirmed_missing = false;
    if (!loaded_model)
    {
        bool new_model = false;
        if (model_manager.LoadModelByName(modelName, new_model))
        {
            loaded_model = model_manager.GetModel(modelName);
        }
        else
        {
            auto disk_models = model_manager.ScanModelDirectory();
            bool found_on_disk = std::any_of(disk_models.begin(), disk_models.end(),
                [&modelName](const json &dm) { return dm.value("id", std::string("")) == modelName; });
            if (!found_on_disk)
            {
                My_Log{My_Log::Level::kWarning}
                    << "[ChatCompletions] Model '" << modelName << "' not found on disk" << std::endl;
                model_confirmed_missing = true;
            }
        }
    }

    if (!loaded_model || !loaded_model->is_loaded)
    {
        json err_json;
        err_json["error"] = "Model '" + modelName + "' not found or unavailable.";
        auto failure_reason = model_manager.GetLastLoadFailureReason();
        if (failure_reason == ModelManager::LoadFailureReason::kInsufficientMemory)
        {
            err_json["failure_reason"] = "insufficient_memory";
            err_json["failure_detail"] = model_manager.GetLastLoadFailureDetail();
        }
        res.status = model_confirmed_missing ? 404 : 500;
        res.set_content(err_json.dump(), ResponseDispatcher::MIMETYPE_JSON);
        return;
    }

    auto handle = loaded_model->context;
    if (!handle || !loaded_model->config)
    {
        My_Log{My_Log::Level::kError}
            << "[ChatCompletions] Model '" << modelName
            << "' has null context or config (is_loaded=" << loaded_model->is_loaded << ")" << std::endl;
        res.status = 500;
        res.set_content(R"({"error": "Model context is not initialized."})", ResponseDispatcher::MIMETYPE_JSON);
        return;
    }
    auto& config = *loaded_model->config;
    handle->Reset();

    if (modelName.find("lora") != std::string::npos)
    {
        std::unordered_map<std::string, float> loraAlphaValue
                {
                        {"lora_alpha", config.getloraAlpha()}
                };
        const char *engineRole{"primary"};
        handle->applyLora(engineRole, config.getloraAdapter());
        handle->setLoraStrength(engineRole, loraAlphaValue);
    }
    handle->SetParamsByConfig(data);

    if (config.getenablePromptDebug() >= 2) {
        My_Log{} << "\n\n=============== data ====================" << std::endl;
        My_Log{} << data << std::endl;
    }

    auto chatHistory = std::make_shared<ChatHistory>(config);
    auto dispatcher = std::make_shared<ResponseDispatcher>(model_manager, *chatHistory, &config);
    auto input_builder = std::make_shared<ModelInputBuilder>(*chatHistory, &config, data);

    if (genieRoutingGateway_ && model_manager.GetRoutingConfig().enabled)
    {
        bool handled_by_cloud = false;
        bool ok = genieRoutingGateway_->HandleChatCompletion(
            data, req, res, *dispatcher, *input_builder, handled_by_cloud);

        if (!ok) return;
        if (handled_by_cloud) return;
    }

    bool is_stream = get_json_value(data, "stream", false);
    const std::string request_id = ResponseTools::generate_uuid4();
    res.set_header("X-Genie-Request-Id", request_id);
    const std::string trace_model_name = config.get_model_name();

    if (is_stream)
    {
        bool routing_enabled = genieRoutingGateway_ && model_manager.GetRoutingConfig().enabled;
        json data_copy = data;

        auto gateway = genieRoutingGateway_;
        auto loaded_model_ref = loaded_model;

        res.set_chunked_content_provider(
            "text/event-stream",
            [this, gateway, data_copy = std::move(data_copy), &req, &res, routing_enabled,
             dispatcher, chatHistory, handle, input_builder, loaded_model_ref, request_id, trace_model_name,
             trace_written = false]
            (size_t offset, httplib::DataSink &sink) mutable -> bool {

                if (offset == 0)
                {
                    ResponseTools::post_stream_data(sink, "data",
                        ResponseTools::statusDataJson("summarizing", "Processing long text, please wait..."));

                    try
                    {
                        bool is_tool = false;
                        auto is_alive_fn = [&sink](void) -> bool {
                            if (!sink.is_writable()) return false;
                            ResponseTools::post_stream_data(sink, "data",
                                ResponseTools::statusDataJson("summarizing",
                                    "Processing long text, please wait..."));
                            return true;
                        };
                        auto& model_input = input_builder->Build(
                            const_cast<json&>(data_copy), is_tool, is_alive_fn);
                        dispatcher->SetSkillMappings(input_builder->GetRuntimeSkillMappings());

                        ResponseTools::post_stream_data(sink, "data",
                            ResponseTools::statusDataJson("prompt_optimized", "Prompt optimization complete",
                                                          input_builder->GetLedger().ToJson()));

                        json temperature = data_copy.value<json>("temp", json());
                        if (!temperature.is_number())
                        {
                            temperature = SamplerFallback(*loaded_model_ref->config, "temp");
                        }
                        bool has_tools = data_copy.contains("tools")
                                      && data_copy["tools"].is_array()
                                      && !data_copy["tools"].empty();
                        if (has_tools) {
                            const float cap = ToolCallTemperatureCap(*loaded_model_ref->config);
                            temperature = temperature.is_number()
                                ? json(std::min(temperature.get<float>(), cap))
                                : json(cap);
                            My_Log{My_Log::Level::kInfo}
                                << "[Tool Call] Adjusting temperature to " << temperature
                                << " for tool calling scenario" << std::endl;
                        }

                        {
                            size_t prompt_tokens = handle->TokenLength(model_input.text_);
                            ApplyOutputSizeBudget(*handle, *loaded_model_ref->config, prompt_tokens, temperature);
                        }

                        dispatcher->Prepare(model_input, is_tool, /*is_stream=*/true, req,
                                                            false, &data_copy);

                        ResponseTools::post_stream_data(sink, "data",
                            ResponseTools::statusDataJson("preparing", "Preparing inference..."));
                    }
                    catch (const std::exception& e)
                    {
                        const std::string what = e.what();
                        My_Log{My_Log::Level::kError}
                            << "[ChatRequestHandler] Build exception in stream: " << what << std::endl;

                        bool is_input_overflow =
                            (what.find("Cannot compress further") != std::string::npos ||
                             what.find("exceed context size") != std::string::npos ||
                             what.find("exceeds context size") != std::string::npos);

                        if (is_input_overflow && routing_enabled && gateway)
                        {
                            My_Log{My_Log::Level::kWarning}
                                << "[ChatRequestHandler] Local input overflow in stream. "
                                << "Triggering inline cloud fallback." << std::endl;
                            ResponseTools::post_stream_data(sink, "data",
                                ResponseTools::statusDataJson("cloud_fallback", "Switching to cloud model..."));
                            bool fallback = gateway->HandleLocalInputOverflow(
                                const_cast<json&>(data_copy), req, res, *dispatcher);
                            if (fallback) return false;
                        }

                        json err_resp = {
                            {"error", {
                                {"message", "Failed to process request: " + what},
                                {"type", "internal_error"},
                                {"code", "build_failed"}
                            }}
                        };
                        std::string err_str = err_resp.dump();
                        ResponseTools::post_stream_data(sink, "data", err_str);
                        ResponseTools::post_stream_data(sink, "data", "[DONE]", true);
                        return false;
                    }
                }

                bool ret = dispatcher->SendResponse(offset, &sink, nullptr, routing_enabled);
                if (!trace_written)
                {
                    trace_written = true;
                    WriteRequestTrace(request_id, trace_model_name, true, *dispatcher, handle,
                                      input_builder->GetLedger(), req.body);
                }

                if (routing_enabled && gateway)
                {
                    if (handle && handle->was_stopped_by_output_limit())
                    {
                        My_Log{My_Log::Level::kWarning}
                            << "[ChatRequestHandler] Local output overflow detected in stream. "
                            << "Triggering inline cloud fallback via existing sink." << std::endl;
                        ResponseTools::post_stream_data(sink, "data",
                            ResponseTools::statusDataJson("cloud_fallback", "Switching to cloud model..."));
                        bool fallback = gateway->HandleLocalOutputOverflow(
                            const_cast<json&>(data_copy), req, res,
                            &sink);
                        if (!fallback)
                        {
                            My_Log{My_Log::Level::kWarning}
                                << "[ChatRequestHandler] Stream fallback failed, sending end markers." << std::endl;
                            ResponseTools::post_stream_data(
                                sink, "data",
                                ResponseTools::responseDataJson("", "length", true));
                            ResponseTools::post_stream_data(sink, "data", "[DONE]", true);
                        }
                        return false;
                    }
                }
                return ret;
            },
            nullptr
        );
    }
    else
    {
        try
        {
            bool is_tool;
            auto &model_input = input_builder->Build(data, is_tool);
            dispatcher->SetSkillMappings(input_builder->GetRuntimeSkillMappings());

            input_builder->GetLedger().WriteHeaders(res);

            json temperature = data.value<json>("temp", json());
            if (!temperature.is_number())
            {
                temperature = SamplerFallback(config, "temp");
            }
            bool has_tools = data.contains("tools") && data["tools"].is_array() && !data["tools"].empty();
            if (has_tools) {
                const float cap = ToolCallTemperatureCap(config);
                temperature = temperature.is_number()
                    ? json(std::min(temperature.get<float>(), cap))
                    : json(cap);
                My_Log{My_Log::Level::kInfo}
                    << "[Tool Call] Adjusting temperature to " << temperature
                    << " for tool calling scenario" << std::endl;
            }

            {
                size_t prompt_tokens = handle->TokenLength(model_input.text_);
                ApplyOutputSizeBudget(*handle, config, prompt_tokens, temperature);
            }

            dispatcher->Prepare(model_input, is_tool, /*is_stream=*/false, req,
                                                false, &data);
            dispatcher->SendResponse(0, nullptr, &res);
            WriteRequestTrace(request_id, trace_model_name, false, *dispatcher, handle,
                          input_builder->GetLedger(), req.body);

            if (handle && handle->was_stopped_by_output_limit()
                    && genieRoutingGateway_ && model_manager.GetRoutingConfig().enabled)
            {
                My_Log{My_Log::Level::kWarning}
                    << "[ChatRequestHandler] Local output overflow detected. "
                    << "Triggering post-execution fallback to cloud." << std::endl;
                bool fallback = genieRoutingGateway_->HandleLocalOutputOverflow(data, req, res);
                if (fallback) return;
            }
        }
        catch (const std::exception &e)
        {
            const std::string what = e.what();
            My_Log{My_Log::Level::kError}
                << "[ChatRequestHandler] Build/inference exception: " << what << std::endl;

            bool is_input_overflow =
                (what.find("Cannot compress further") != std::string::npos ||
                 what.find("exceed context size") != std::string::npos ||
                 what.find("exceeds context size") != std::string::npos);

            if (is_input_overflow && genieRoutingGateway_ && model_manager.GetRoutingConfig().enabled)
            {
                My_Log{My_Log::Level::kWarning}
                    << "[ChatRequestHandler] Local input overflow detected. "
                    << "Triggering pre-execution fallback to cloud." << std::endl;

                if (genieRoutingGateway_->HandleLocalInputOverflow(data, req, res, *dispatcher))
                {
                    return;
                }
            }

            json err_resp = {
                {"error", {
                    {"message", "Failed to process request: " + what},
                    {"type", "internal_error"},
                    {"code", "build_failed"}
                }}
            };
            res.status = 500;
            res.set_content(err_resp.dump(), ResponseDispatcher::MIMETYPE_JSON);
            return;
        }
    }
}

void ChatRequestHandler::FetchProfile(const httplib::Request &req, httplib::Response &res)
{
    const std::string model_name = req.get_param_value("model");
    const auto queried = ResolveQueriedModels(model_manager, model_name, res);
    if (!model_name.empty() && queried.empty())
    {
        return;
    }

    json entries = json::array();
    for (const auto &loaded: queried)
    {
        json profile = loaded->context->HandleProfile();
        if (profile.empty())
        {
            if (!model_name.empty())
            {
                res.status = 503;
                res.set_content(R"({"error": "Profile data not ready", "reason": "no_profile_data"})",
                                ResponseDispatcher::MIMETYPE_JSON);
                return;
            }
            profile = json::object();
            profile["reason"] = "no_profile_data";
        }
        profile["model"] = loaded->config->get_model_name();
        entries.push_back(std::move(profile));
    }
    json result = {{"models", std::move(entries)}, {"loaded_models", queried.size()}};
    res.set_content(json_to_str(result), ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::ImageGenerate(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    res.set_content("", ResponseDispatcher::MIMETYPE_JSON);
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

void ChatRequestHandler::FetchModelStatus(const httplib::Request &req, httplib::Response &res)
{
    json result;

    json slots = json::array({
        { {"device", "npu"}, {"model", ""}, {"is_loaded", false}, {"backend", ""} },
        { {"device", "gpu"}, {"model", ""}, {"is_loaded", false}, {"backend", ""} },
        { {"device", "cpu"}, {"model", ""}, {"is_loaded", false}, {"backend", ""} }
    });
    const auto loaded_models = model_manager.GetAllLoadedModels();
    size_t loaded_model_count = 0;

    for (const auto &loaded_model: loaded_models)
    {
        if (!loaded_model || !loaded_model->is_loaded || !loaded_model->config)
        {
            continue;
        }
        ++loaded_model_count;
        for (const auto &family: loaded_model->occupied_families)
        {
            for (auto &slot: slots)
            {
                if (slot["device"].get<std::string>() != family || slot["is_loaded"].get<bool>())
                {
                    continue;
                }
                slot["model"] = loaded_model->config->get_model_name();
                slot["is_loaded"] = true;
                slot["backend"] = loaded_model->backend;
                break;
            }
        }
    }

    result["slots"] = std::move(slots);
    result["loaded_models"] = loaded_model_count;
    res.set_content(result.dump(), ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::UnloadModel(const httplib::Request &req, httplib::Response &res)
{
    const std::string model_name = ModelNameFromBody(req.body);
    if (model_name.empty())
    {
        res.status = 400;
        res.set_content(R"({"error": "model is required"})", ResponseDispatcher::MIMETYPE_JSON);
        return;
    }
    if (!model_manager.UnloadModelByName(model_name))
    {
        json err = {{"error", "model '" + model_name + "' is not loaded"}};
        res.status = 404;
        res.set_content(err.dump(), ResponseDispatcher::MIMETYPE_JSON);
        return;
    }
    json ok = {{"status", "unloaded"}, {"model", model_name}};
    res.set_content(ok.dump(), ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}
