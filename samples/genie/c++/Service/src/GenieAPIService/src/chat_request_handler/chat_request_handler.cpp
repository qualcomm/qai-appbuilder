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

namespace fs = std::filesystem;

namespace
{
// Computes the output-token budget (context window minus consumed prompt tokens, clamped between
// the configured minimum output and the full context size) and applies it together with the
// sampling params to the model handle. Shared by the stream and non-stream inference paths.
//
// Returns the model's own config.json sampler value for `key` if it is present and numeric,
// otherwise a null json. There is no literal fallback here: a key that is neither requested
// nor configured must be omitted from the SetParamsByConfig() call entirely (see
// ApplyOutputSizeBudget) rather than overwritten with an arbitrary constant.
json SamplerFallback(const ModelInstanceConfig &config, const std::string &key)
{
    auto &sampler = config.sampler();
    return (sampler.contains(key) && sampler.at(key).is_number()) ? sampler.at(key) : json();
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

    // 只把有合法数值的采样参数塞进去；缺省的 key 直接不出现在这个 json 里，
    // SetParamsByConfig() 自身按 model_config_.sampler() 的 key 遍历、j 不含该 key 即 continue，
    // 会自然跳过未设置的参数，保留 SDK 创建 Dialog 时的默认值，而不是被改写成任意常量。
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
} // namespace

ChatRequestHandler::ChatRequestHandler(GenieService *srv) :
        model_manager(*srv->modelManager),
        srv_{srv}
{
    // 按配置初始化 GenieRoutingGateway
    // 仅当 routing.enabled=true 时才初始化 GenieRoutingGateway，与 HandleChatCompletion 中的
    // 检查条件（routing_config_.enabled）保持一致，避免逻辑不对称。
    // 说明：cloud_config.enabled 仅控制云端客户端是否可用，不单独触发 GenieRoutingGateway 初始化；
    //       GenieRoutingGateway 的启用入口统一由 routing.enabled 控制。
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

    // 辅助函数：字符串转小写（用于大小写不敏感匹配）
    auto to_lower = [](const std::string &s) -> std::string {
        std::string r = s;
        std::transform(r.begin(), r.end(), r.begin(),
                       [](unsigned char c) { return static_cast<unsigned char>(std::tolower(c)); });
        return r;
    };

    // 单模型语义：本仓库已删除并发多模型托管设计（原 loaded_models_ 注册表），此接口只应
    // 报告当前 -c 指定并成功加载的这一个模型。大小写不敏感匹配用于处理 -c 参数目录名大小写
    // 与模型 name 字段不一致的情况（例如 -c "qwen3-8B-8K/config.json" 加载后名称为
    // "qwen3-8B-8K"，但目录名可能是 "Qwen3-8B-8K"）。
    auto current = model_manager.GetDefaultModel();
    std::string current_name_lower, current_dir_lower;
    if (current && current->config)
    {
        current_name_lower = to_lower(current->config->get_model_name());
        fs::path p(current->config->get_model_path());
        current_dir_lower = to_lower(p.filename().generic_string());
    }

    // 优先扫描磁盘，返回 model_root_ 下所有含 config.json 的子目录
    std::vector<json> disk_models = model_manager.ScanModelDirectory();

    if (!disk_models.empty())
    {
        // 有磁盘扫描结果：返回全部磁盘模型，只把当前模型标注为已加载
        bool current_found_on_disk = false;
        for (auto &m : disk_models)
        {
            m["object"]   = "model";
            m["created"]  = now_ts;
            m["owned_by"] = "owner";
            const std::string disk_id_lower = to_lower(m["id"].get<std::string>());
            bool is_current = current && (
                (!current_name_lower.empty() && disk_id_lower == current_name_lower) ||
                (!current_dir_lower.empty() && disk_id_lower == current_dir_lower));
            m["is_loaded"] = is_current;
            if (is_current)
            {
                current_found_on_disk = true;
                m["context_length"] = current->config->get_context_size();
                m["backend"] = current->backend;
                m["device"] = current->device;
            }
            model_list.push_back(m);
        }

        // 当前模型的 name 可能是 config.json 里声明的运行时 ID，与磁盘目录名不一致；
        // 磁盘扫描存在时也要把这种情况下的当前模型补充到 /models。
        if (current && current->config && !current_found_on_disk)
        {
            json m;
            m["id"]             = current->config->get_model_name();
            m["object"]         = "model";
            m["created"]        = now_ts;
            m["owned_by"]       = "owner";
            m["is_loaded"]      = true;
            m["context_length"] = current->config->get_context_size();
            m["backend"]        = current->backend;
            m["device"]         = current->device;
            model_list.push_back(m);
        }
    }
    else if (current && current->config)
    {
        // model_root_ 未配置或为空：回退到只返回当前已加载模型
        json m;
        m["id"]             = current->config->get_model_name();
        m["object"]         = "model";
        m["created"]        = now_ts;
        m["owned_by"]       = "owner";
        m["is_loaded"]      = true;
        m["context_length"] = current->config->get_context_size();
        m["backend"]        = current->backend;
        m["device"]         = current->device;
        model_list.push_back(m);
    }

    models["data"]   = model_list;
    models["object"] = "list";
    res.set_content(models.dump(2), ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::ContextSize(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    std::string model_name = data.value("model", "");

    json contextSize;
    auto loaded = model_manager.GetModel(model_name);
    if (!loaded) {
        loaded = model_manager.GetDefaultModel();
    }

    if (loaded) {
        contextSize["contextsize"] = loaded->config->get_context_size();
    } else {
        contextSize["contextsize"] = 0;
    }
    res.set_content(contextSize.dump(2), ResponseDispatcher::MIMETYPE_JSON);
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
        // Spawn a shutdown thread: unload model (release NPU), stop server, exit.
        // The 2-second delay ensures httplib flushes the response.
        auto srv = srv_;
        std::thread([srv]() {
            std::this_thread::sleep_for(std::chrono::seconds(2));
            srv->ServiceStop();
        }).detach();
    }
}

void ChatRequestHandler::ModelStop(const httplib::Request &req, httplib::Response &res)
{
    json data = json::parse(req.body, nullptr, false);
    std::string text = data.value("text", "");
    std::string model_name = data.value("model", "");

    if (text == "stop")
    {
        auto loaded = model_manager.GetModel(model_name);
        if (!loaded) {
            loaded = model_manager.GetDefaultModel();
        }

        if (loaded && loaded->context) {
            loaded->context->Stop();
        }
    }
    res.set_content("", ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::ClearMessage(const httplib::Request &req, httplib::Response &res)
{
    // History is stateless per request now.
    res.set_content("", ResponseDispatcher::MIMETYPE_JSON);
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
    std::string model_name = data.value("model", "");
    
    auto loaded = model_manager.GetModel(model_name);
    if (!loaded) {
        loaded = model_manager.GetDefaultModel();
    }

    if (!loaded) {
        res.status = 500;
        res.set_content(R"({"error": "No model loaded."})", ResponseDispatcher::MIMETYPE_JSON);
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

    // 剥离 QAIAgentForge 添加的 "local::" 前缀（GenieAPIService 内部使用裸模型名称作为 key）
    static const std::string kLocalPrefix = "local::";
    if (modelName.size() > kLocalPrefix.size() &&
        modelName.substr(0, kLocalPrefix.size()) == kLocalPrefix)
    {
        modelName = modelName.substr(kLocalPrefix.size());
    }
    
    // 单模型语义：本仓库已删除并发多模型托管设计（原 loaded_models_ 注册表）。服务只维护
    // 当前 -c 指定并成功加载的这一个模型；请求未指定模型名，或指定名称与当前模型匹配时直接
    // 使用它，否则通过 LoadModelByName() 顺序切换（先卸载旧模型再加载新模型，不重启进程，
    // 不是并发托管）。
    auto loaded_model = model_manager.GetModel(modelName);
    if (!loaded_model && modelName.empty()) {
        loaded_model = model_manager.GetDefaultModel();
    }

    bool model_confirmed_missing = false;   // 本次请求内已通过磁盘扫描确定性地证明该模型名不存在
    if (!loaded_model && !modelName.empty())
    {
        bool new_model = false;
        if (model_manager.LoadModelByName(modelName, new_model))
        {
            loaded_model = model_manager.GetModel(modelName);
            if (!loaded_model)
            {
                // LoadModelByName 成功但 GetModel 精确/大小写不敏感匹配失败，回退取当前模型
                loaded_model = model_manager.GetDefaultModel();
            }
        }
        else
        {
            // 区分"模型名在磁盘上确实不存在"（404）与"存在但加载失败"（500）
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

    // LoadModelByName 为同步调用，上面的分支已给出确定性结论（找到即 loaded_model 非空，
    // 找不到即仍为空），不存在"稍后可能出现"的中间态可等待，因此不再引入轮询等待。

    if (!loaded_model || !loaded_model->is_loaded)
    {
        // 附加可被程序化识别的失败原因（目前主要区分"内存不足"与其它，见 ModelManager::LoadFailureReason），
        // 供测试脚本/客户端区分"服务优雅拒绝加载"与其它未知失败，不改变现有 error 字段语义（向后兼容）。
        json err_json;
        err_json["error"] = modelName.empty()
            ? "No model available. Please load a model first."
            : "Model '" + modelName + "' not found or unavailable.";
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
    // 防御性检查：context 和 config 不应为空（is_loaded=true 时应已设置），
    // 但为了避免潜在的空指针解引用，在此处显式检查
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

    // Create objects for request handling
    // Use shared_ptr for objects that need to survive for streaming response callback
    auto chatHistory = std::make_shared<ChatHistory>(config);
    // 修复：传入 &config（per-model 的 ModelInstanceConfig）给 ResponseDispatcher，
    // 使 ResetProcessor() 使用正确的 prompt type 而非全局 IModelConfig 的 prompt type
    auto dispatcher = std::make_shared<ResponseDispatcher>(model_manager, *chatHistory, &config);
    
    // 将 request_data 传递给 ModelInputBuilder，以便 PromptOptimizer 可以提取 Skill 描述
    // 使用 shared_ptr 以便在 stream 路径中被 lambda 捕获（Build() 需在 lambda 内执行以支持摘要保活）
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

    if (is_stream)
    {
        // ── Stream 路径 ──────────────────────────────────────────────────────────
        // 长文本摘要化在 Build() 中同步执行，可能耗时数分钟。
        // 若在 set_chunked_content_provider 注册之前调用 Build()，客户端在此期间
        // 收不到任何 HTTP 数据，会因超时而断开连接。
        //
        // 修复方案：将 Build() + Prepare() 移入 chunked content provider 的 lambda 中，
        // 在 offset==0 时先向客户端发送 "summarizing" 状态帧（保活），再执行 Build()。
        // 这样 HTTP 响应头立即发出，客户端不会超时。
        bool routing_enabled = genieRoutingGateway_ && model_manager.GetRoutingConfig().enabled;
        json data_copy = data;

        // 捕获 gateway 的 shared_ptr 副本，延长生命周期至 lambda 结束
        auto gateway = genieRoutingGateway_;
        // 捕获 loaded_model，保证 config（ModelInstanceConfig&）的生命周期
        auto loaded_model_ref = loaded_model;

        res.set_chunked_content_provider(
            "text/event-stream",
            [this, gateway, data_copy = std::move(data_copy), &req, &res, routing_enabled,
             dispatcher, chatHistory, handle, input_builder, loaded_model_ref]
            (size_t offset, httplib::DataSink &sink) mutable -> bool {

                // offset==0：执行 Build() + Prepare()，并发送保活状态帧
                if (offset == 0)
                {
                    // 立即发送 "summarizing" 状态帧，让客户端知道服务端正在处理
                    // （Phase -1 摘要推理可能耗时较长，此帧防止客户端超时断开）
                    ResponseTools::post_stream_data(sink, "data",
                        ResponseTools::statusDataJson("summarizing", "Processing long text, please wait..."));

                    try
                    {
                        bool is_tool = false;
                        // 连接存活检测 + 保活帧发送：
                        // 每次摘要 Map 推理前调用，同时完成两件事：
                        //   1. 向客户端发送 "summarizing" 保活帧，防止 httpx read 超时（默认 300s）
                        //      每次 Map 推理约 1-2 分钟，多个 chunk 累计可能超过 300s
                        //   2. 检测 sink 是否可写，若客户端已断开则返回 false 提前终止摘要
                        auto is_alive_fn = [&sink](void) -> bool {
                            if (!sink.is_writable()) return false;
                            // 发送保活状态帧，重置客户端的 read 超时计时器
                            ResponseTools::post_stream_data(sink, "data",
                                ResponseTools::statusDataJson("summarizing",
                                    "Processing long text, please wait..."));
                            return true;
                        };
                        auto& model_input = input_builder->Build(
                            const_cast<json&>(data_copy), is_tool, is_alive_fn);

                        // PromptLedger 可观测（流式路径）：流式响应头已在 chunked provider 进入时
                        // 发出，无法再写响应头，改为在 Build() 完成后发一帧 status="prompt_optimized"，
                        // payload 由 PromptLedger::ToJson() 生成，与非流式响应头共用同一套字段口径。
                        ResponseTools::post_stream_data(sink, "data",
                            ResponseTools::statusDataJson("prompt_optimized", "Prompt optimization complete",
                                                          input_builder->GetLedger().ToJson()));

                        // 动态调整 temperature：请求显式传入优先；否则回退到 config.json 自身的
                        // sampler.temp；两者都没有合法数值时保持为 null，ApplyOutputSizeBudget 会
                        // 完全不设置该参数，不对未配置的模型注入与其无关的任意默认值。
                        json temperature = data_copy.value<json>("temp", json());
                        if (!temperature.is_number())
                        {
                            temperature = SamplerFallback(*loaded_model_ref->config, "temp");
                        }
                        bool has_tools = data_copy.contains("tools")
                                      && data_copy["tools"].is_array()
                                      && !data_copy["tools"].empty();
                        if (has_tools) {
                            const auto& opt_config = model_manager.GetPromptOptimizationConfig();
                            temperature = temperature.is_number()
                                ? json(std::min(temperature.get<float>(), opt_config.tool_call_temperature))
                                : json(opt_config.tool_call_temperature);
                            My_Log{My_Log::Level::kInfo}
                                << "[Tool Call] Adjusting temperature to " << temperature
                                << " for tool calling scenario" << std::endl;
                        }

                        {
                            size_t prompt_tokens = handle->TokenLength(model_input.text_);
                            ApplyOutputSizeBudget(*handle, *loaded_model_ref->config, prompt_tokens, temperature);
                        }

                        // Layer2 内部隐形自纠正重试需要复用本次请求的原始消息序列（未经
                        // PreFilter/压缩，仅经过上方的控制字符 sanitize）重新走一遍 Build()，
                        // 因此把 data_copy 的地址传给 Prepare() 保存快照；dispatcher 内部
                        // 会做深拷贝，data_copy 后续被其它逻辑修改不影响已保存的快照。
                        dispatcher->Prepare(model_input, is_tool, /*is_stream=*/true, req,
                                            /*is_dll_mode=*/false, &data_copy);

                        // 发送 "preparing" 状态帧（Build 完成，即将开始主推理）
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
        // ── 非 Stream 路径（保持原有同步逻辑）────────────────────────────────────
        try
        {
            bool is_tool;
            auto &model_input = input_builder->Build(data, is_tool);

            // PromptLedger 可观测（非流式路径）：Build() 完成后、写响应体前写入
            // X-Genie-Prompt-* 全部字段；与流式路径的 status="prompt_optimized" 帧共用同一套
            // PromptLedger::ToJson()/字段口径。
            input_builder->GetLedger().WriteHeaders(res);

            // 动态调整 temperature：同上（见流式路径注释），请求优先，否则回退到 config.json，
            // 两者都没有合法数值时保持为 null，完全不设置该参数。
            json temperature = data.value<json>("temp", json());
            if (!temperature.is_number())
            {
                temperature = SamplerFallback(config, "temp");
            }
            bool has_tools = data.contains("tools") && data["tools"].is_array() && !data["tools"].empty();
            if (has_tools) {
                const auto& opt_config = model_manager.GetPromptOptimizationConfig();
                temperature = temperature.is_number()
                    ? json(std::min(temperature.get<float>(), opt_config.tool_call_temperature))
                    : json(opt_config.tool_call_temperature);
                My_Log{My_Log::Level::kInfo}
                    << "[Tool Call] Adjusting temperature to " << temperature
                    << " for tool calling scenario" << std::endl;
            }

            {
                size_t prompt_tokens = handle->TokenLength(model_input.text_);
                ApplyOutputSizeBudget(*handle, config, prompt_tokens, temperature);
            }

            dispatcher->Prepare(model_input, is_tool, /*is_stream=*/false, req,
                                /*is_dll_mode=*/false, &data);
            dispatcher->SendResponse(0, nullptr, &res);

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
    // GetDefaultModel 为同步调用，找不到即不存在"稍后可能出现"的中间态，因此不再引入轮询等待。
    auto loaded = model_manager.GetDefaultModel();
    if (!loaded || !loaded->context)
    {
        res.status = 503;
        res.set_content(R"({"error": "No model loaded or model unavailable"})", ResponseDispatcher::MIMETYPE_JSON);
        return;
    }
    json result = loaded->context->HandleProfile();
    if (!result.empty())
    {
        res.set_content(json_to_str(result), ResponseDispatcher::MIMETYPE_JSON);
        res.status = 200;
    }
    else
    {
        // 各后端的 HandleProfile() 在 profile 数据尚未生成时（例如推理尚未真正开始）
        // 均返回空 json{}，此前既不设置状态码也不设置 body，httplib::Response 会把
        // 默认状态当成 200 发出，导致"200 但 body 为空"这一违反 HTTP 语义的假象。
        // 显式区分于上面"无模型加载"的 503，让客户端可以通过 reason 字段区分两种场景。
        res.status = 503;
        res.set_content(R"({"error": "Profile data not ready", "reason": "no_profile_data"})",
                         ResponseDispatcher::MIMETYPE_JSON);
    }
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
    result["loading"] = std::to_string(!model_manager.IsLoaded());
    res.set_content(result.dump(), ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}

void ChatRequestHandler::UnloadModel(const httplib::Request &req, httplib::Response &res)
{
    model_manager.UnloadModel();
    res.set_content("", ResponseDispatcher::MIMETYPE_JSON);
    res.status = 200;
}
