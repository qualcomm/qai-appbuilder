//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "GenieAPILibrary.h"
#include "model/model_manager.h"
#include "chat_request_handler/chat_request_handler.h"
#include "chat_request_handler/model_input_builder.h"
#include "chat_history/chat_history.h"
#include "response/response_dispatcher.h"
#include "utils.h"
#include <GenieCommon.h>
#include <filesystem>

#ifdef WIN32
#include <direct.h>
#else
#include <unistd.h>
#endif

// Internal implementation class
class QInterfaceImpl {
public:
    std::unique_ptr<std::thread> inference_thread_;
    std::unique_ptr<std::thread> load_thread_;
    std::string last_error;
    std::mutex mutex;
    std::mutex inference_mutex;
    std::mutex load_mutex;
    std::condition_variable load_cv;
    bool load_done = false;
    bool load_result = false;
    bool model_loaded = false;
    bool stop_requested = false;
    
    std::string current_prompt;
    std::string generated_token;
    std::function<bool(const std::string&)> stream_callback;
    
    // Model management components
    std::unique_ptr<ModelManager> model_manager;
    std::unique_ptr<ChatHistory> chat_history;
    std::unique_ptr<ResponseDispatcher> response_dispatcher;
    std::unique_ptr<ModelInputBuilder> input_builder;
    
    // Configuration
    std::string config_file;
    std::string lora_adapter;
    std::string log_file;
    int log_level = 2;
    int num_response = 10;
    int min_output_num = 1;
    float lora_alpha = 1.0f;
    bool output_all_text = false;
    bool enable_thinking = false;
    
    int current_status = init;
    int generate_status = completed;
    
    bool InitializeModelManager(const std::string& config_json, Level level) {
        try {
            // config_json 可以是两种形式：
            // 1) 内联 JSON 文本(SampleApp 宏编译内置的 QNN dialog 字符串)——保留严格的 dialog 必填校验
            // 2) 磁盘上真实的 config.json 路径(QNN/MNN/GGUF 均可)——跳过该校验,交给底层引擎自己识别
            std::string trimmed = config_json;
            size_t start = trimmed.find_first_not_of(" \t\n\r");
            trimmed = (start != std::string::npos) ? trimmed.substr(start) : std::string();
            bool is_inline_json = (!trimmed.empty() && trimmed[0] == '{');

            if (is_inline_json) {
                json config_data = json::parse(config_json);
                if (!config_data.contains("dialog")) {
                    last_error = "Config JSON must contain 'dialog' section";
                    return false;
                }
            } else if (!File::IsFileExist(config_json)) {
                last_error = "config is neither an inline JSON object nor an existing config file path: " + config_json;
                return false;
            }

            config_file = config_json;
            
            // Create model config
            IModelConfig model_config;
            model_config.config_file_ = config_file;
            model_config.loraAdapter = lora_adapter;
            model_config.loraAlpha = lora_alpha;
            model_config.num_response_ = num_response;
            model_config.minOutputNum = min_output_num;
            model_config.outputAllText = output_all_text;
            model_config.enableThinking = enable_thinking;
            model_config.log_level_ = level;
            
            // Create model manager
            model_manager = std::make_unique<ModelManager>(std::move(model_config));
            
            // Don't call InitializeConfig since we're using JSON string
            // We'll set the necessary fields directly
            model_manager->config_file_ = config_file;
            
            // Create chat history
            chat_history = std::make_unique<ChatHistory>(*model_manager);
            
            // Create response dispatcher
            response_dispatcher = std::make_unique<ResponseDispatcher>(*model_manager, *chat_history);
            
            return true;
            
        } catch (const std::exception& e) {
            last_error = std::string("Model manager initialization failed: ") + e.what();
            return false;
        }
    }

    // primary 模型（--config 指定）加载成功后调用：若 RootDir/service_config.json 存在，
    // 附加加载其中的 qnn/NPU 模型条目；附加加载不影响 primary 已加载成功这一结果，
    // 且会把 default_model_name_ 复位回 primary，避免被最后加载的额外模型覆盖。
    void TryAutoLoadAdditionalQnnModels() {
        if (!model_manager) {
            return;
        }
        std::string service_config_path = (std::filesystem::path(RootDir) / "service_config.json").generic_string();
        if (!File::IsFileExist(service_config_path)) {
            return;
        }
        bool multi_ok = model_manager->LoadAllModelsFromConfig("qnn");
        My_Log{My_Log::Level::kInfo} << "[api_loadmodel] LoadAllModelsFromConfig(qnn): "
                                      << (multi_ok ? "loaded additional qnn model(s)" : "no additional qnn model loaded")
                                      << std::endl;
        model_manager->SetDefaultModel(model_manager->model_name_);
        response_dispatcher = std::make_unique<ResponseDispatcher>(*model_manager, *chat_history,
                                                                     model_manager->GetDefaultInstanceConfig());
    }
};

// Builds the OpenAI-style request JSON from a raw prompt, reused by both api_Generate overloads:
// if the prompt is itself valid JSON it is used as-is, otherwise it is wrapped into a single
// user message before the stream flag is set.
static json BuildRequestDataFromPrompt(const std::string& prompt, bool stream) {
    json request_data;
    try {
        request_data = json::parse(prompt);
    } catch (...) {
        request_data["messages"] = json::array();
        json message;
        message["role"] = "user";
        json content;
        content["question"] = prompt;
        content["image"] = "";
        content["audio"] = "";
        message["content"] = content;
        request_data["messages"].push_back(message);
    }
    request_data["stream"] = stream;
    return request_data;
}

// api_interface implementation

api_interface::api_interface(std::string& config, Level level) : config_(config), level_{level}
{
    // Initialize RootDir and CurrentDir for DLL usage
    if (RootDir.empty()) {
        char buffer[1024];
#ifdef WIN32
        if (_getcwd(buffer, sizeof(buffer))) {
            CurrentDir = buffer;
        }
#else
        if (getcwd(buffer, sizeof(buffer))) {
            CurrentDir = buffer;
        }
#endif
        RootDir = CurrentDir;  // Set RootDir to current directory for DLL
    }
}

api_interface::~api_interface() {
    if (impl_) {
        delete impl_;
        impl_ = nullptr;
    }
}

bool api_interface::api_loadmodel(const std::string& model_path, std::vector<std::string>& model_name, const std::string& hwinfo) {
    if (!impl_) {
        impl_ = new QInterfaceImpl();
        
        if (!impl_->InitializeModelManager(config_, level_)) {
            My_Log{My_Log::Level::kError} << "[api_loadmodel] InitializeModelManager failed: " << impl_->last_error << std::endl;
            status = error;
            impl_->current_status = error;
            return false;
        }
    }
    
    try {
        std::lock_guard<std::mutex> lock(impl_->inference_mutex);
        status = loading;
        impl_->current_status = loading;
        
        // config_file_ 要么是内联 JSON 文本(以 '{' 开头),要么是磁盘上真实存在的 config.json 路径,
        // 两种情况都走 LoadSingleModel() 直连加载(等价于 GenieAPIService.exe -c 的单模型加载路径),
        // 都能覆盖 QNN/MNN/GGUF 三种后端;其余情况才走多模型按名查找的 LoadModelByName。
        std::string trimmed = impl_->model_manager->config_file_;
        size_t start = trimmed.find_first_not_of(" \t\n\r");
        if (start != std::string::npos) {
            trimmed = trimmed.substr(start);
        }
        
        bool is_direct_config = (!trimmed.empty() && trimmed[0] == '{') || File::IsFileExist(trimmed);
        
        if (is_direct_config) {
            // 直连加载:model_path 是模型目录,model_name_/model_root_ 按目录名/父目录推导,
            // 与 ModelManager::InitializeConfig() 里 -c 单模型模式的推导方式保持一致。
            impl_->model_manager->model_path_ = model_path;
            impl_->model_manager->model_name_ = std::filesystem::path(model_path).filename().generic_string();
            impl_->model_manager->model_root_ = std::filesystem::path(model_path).parent_path().generic_string();
            // Keep config_file_ as-is (inline JSON text or disk path) - don't change it!
            
            if (!impl_->model_manager->LoadSingleModel()) {
                impl_->last_error = "Failed to load model";
                My_Log{My_Log::Level::kError} << "[api_loadmodel] LoadSingleModel failed" << std::endl;
                status = error;
                impl_->current_status = error;
                return false;
            }
            if (impl_->response_dispatcher) {
                impl_->response_dispatcher->ResetProcessor();
            }
        } else {
            // Using file path config - call LoadModelByName
            bool first_load = false;
            if (!impl_->model_manager->LoadModelByName(model_path, first_load)) {
                impl_->last_error = "Failed to load model: " + model_path;
                My_Log{My_Log::Level::kError} << "[api_loadmodel] LoadModelByName failed: " << model_path << std::endl;
                status = error;
                impl_->current_status = error;
                return false;
            }
            
            if (first_load && impl_->response_dispatcher) {
                impl_->response_dispatcher->ResetProcessor();
            }
        }
        
        impl_->TryAutoLoadAdditionalQnnModels();
        
        impl_->model_loaded = true;
        status = loaded;
        impl_->current_status = loaded;
        return true;
        
    } catch (const std::exception& e) {
        impl_->last_error = e.what();
        My_Log{My_Log::Level::kError} << "[api_loadmodel] Exception caught: " << e.what() << std::endl;
        status = error;
        impl_->current_status = error;
        return false;
    }
}

bool api_interface::api_loadmodel_async(const std::string& model_path, std::vector<std::string>& model_name, const std::string& hwinfo) {
    if (!impl_) {
        impl_ = new QInterfaceImpl();
        if (!impl_->InitializeModelManager(config_, level_)) {
            My_Log{My_Log::Level::kError} << "[api_loadmodel_async] InitializeModelManager failed: " << impl_->last_error << std::endl;
            status = error;
            impl_->current_status = error;
            return false;
        }
    }

    // Reset load state
    {
        std::lock_guard<std::mutex> lk(impl_->load_mutex);
        impl_->load_done = false;
        impl_->load_result = false;
    }

    status = loading;
    impl_->current_status = loading;

    // Capture by value for thread safety
    std::string mp = model_path;
    std::string hw = hwinfo;

    impl_->load_thread_ = std::make_unique<std::thread>([this, mp, hw]() {
        bool result = false;
        try {
            std::lock_guard<std::mutex> lock(impl_->inference_mutex);
            bool first_load = false;
            result = impl_->model_manager->LoadModelByName(mp, first_load);
            if (result) {
                if (first_load && impl_->response_dispatcher) {
                    impl_->response_dispatcher->ResetProcessor();
                }
                impl_->TryAutoLoadAdditionalQnnModels();
                impl_->model_loaded = true;
                this->status = ::loaded;
                impl_->current_status = ::loaded;
            } else {
                impl_->last_error = "Failed to load model: " + mp;
                this->status = ::error;
                impl_->current_status = ::error;
                My_Log{My_Log::Level::kError} << "[api_loadmodel_async] LoadModelByName failed: " << mp << std::endl;
            }
        } catch (const std::exception& e) {
            My_Log{My_Log::Level::kError} << "[api_loadmodel_async] Exception caught: " << e.what() << std::endl;
            impl_->last_error = std::string("Exception in model load: ") + e.what();
            this->status = ::error;
            impl_->current_status = ::error;
        } catch (...) {
            My_Log{My_Log::Level::kError} << "[api_loadmodel_async] Unknown exception caught" << std::endl;
            impl_->last_error = "Unknown exception in model load";
            this->status = ::error;
            impl_->current_status = ::error;
        }

        // Notify waiting threads
        {
            std::lock_guard<std::mutex> lk(impl_->load_mutex);
            impl_->load_result = result;
            impl_->load_done = true;
        }
        impl_->load_cv.notify_all();
    });

    return true;
}

bool api_interface::api_wait_loaded(int timeout_ms) {
    if (!impl_) {
        return false;
    }

    std::unique_lock<std::mutex> lk(impl_->load_mutex);

    auto pred = [this]() { return impl_->load_done; };

    if (timeout_ms < 0) {
        // Wait indefinitely, but print progress every 5 seconds
        while (!impl_->load_done) {
            impl_->load_cv.wait_for(lk, std::chrono::seconds(5), pred);
            if (!impl_->load_done) {
                My_Log{My_Log::Level::kInfo} << "[api_wait_loaded] Still loading model, please wait..." << std::endl;
            }
        }
    } else {
        impl_->load_cv.wait_for(lk, std::chrono::milliseconds(timeout_ms), pred);
    }

    if (!impl_->load_done) {
        My_Log{My_Log::Level::kWarning} << "[api_wait_loaded] Timed out waiting for model load" << std::endl;
        return false;
    }

    // Join the load thread
    if (impl_->load_thread_ && impl_->load_thread_->joinable()) {
        lk.unlock();
        impl_->load_thread_->join();
        impl_->load_thread_.reset();
        lk.lock();
    }

    return impl_->load_result;
}

bool api_interface::api_unloadmodel() {
    if (!impl_ || !impl_->model_manager) {
        return true;
    }
    
    try {
        std::lock_guard<std::mutex> lock(impl_->inference_mutex);
        
        impl_->model_manager->UnloadModel();
        impl_->model_loaded = false;
        status = unloaded;
        impl_->current_status = unloaded;
        
        My_Log{My_Log::Level::kInfo} << "Model unloaded" << std::endl;
        return true;
        
    } catch (const std::exception& e) {
        impl_->last_error = e.what();
        return false;
    }
}

int api_interface::api_status() {
    if (!impl_) {
        return init;
    }
    return impl_->current_status;
}

std::string api_interface::api_Generate(const std::string& prompt) {
    if (!impl_ || !impl_->model_manager || !impl_->model_manager->IsLoaded()) {
        return build_response_json("", prompt, false, false);
    }
    
    try {
        std::lock_guard<std::mutex> lock(impl_->inference_mutex);
        
        status = inference;
        impl_->current_status = inference;
        impl_->generate_status = generating;
        
        auto model_handle = impl_->model_manager->GetDefaultModelHandle().lock();
        if (!model_handle) {
            impl_->last_error = "Model context unavailable";
            impl_->generate_status = failed;
            return build_response_json("", prompt, false, false);
        }
        
        json request_data = BuildRequestDataFromPrompt(prompt, false);
        
        // Set parameters
        model_handle->SetParamsByConfig(request_data);
        
        // Build model input
        bool is_tool = false;
        impl_->input_builder = std::make_unique<ModelInputBuilder>(*impl_->chat_history,  impl_->model_manager->GetDefaultInstanceConfig(), nullptr);
        auto& model_input = impl_->input_builder->Build(request_data, is_tool);
        
        // Prepare response dispatcher
        httplib::Request dummy_req;
        impl_->response_dispatcher->Prepare(model_input, is_tool, false, dummy_req, true);
        
        // Perform inference
        httplib::Response dummy_res;
        if (!impl_->response_dispatcher->SendResponse(0, nullptr, &dummy_res)) {
            impl_->last_error = "Inference failed";
            impl_->generate_status = failed;
            status = loaded;
            impl_->current_status = loaded;
            return build_response_json("", prompt, false, false);
        }
        
        std::string response_body = dummy_res.body;
        
        impl_->generate_status = completed;
        status = loaded;
        impl_->current_status = loaded;
        
        return build_response_json(response_body, prompt, false, false);
        
    } catch (const std::exception& e) {
        impl_->last_error = e.what();
        impl_->generate_status = failed;
        status = loaded;
        impl_->current_status = loaded;
        return build_response_json("", prompt, false, false);
    }
}

std::string api_interface::api_Generate(const std::string& prompt, std::function<bool(const std::string& chunk)> callback) {
    if (!impl_ || !impl_->model_manager || !impl_->model_manager->IsLoaded()) {
        return build_response_json("", prompt, false, true);
    }
    
    try {
        std::lock_guard<std::mutex> lock(impl_->inference_mutex);
        status = inference;
        impl_->current_status = inference;
        impl_->generate_status = generating;
        impl_->stream_callback = callback;
        
        auto model_handle = impl_->model_manager->GetDefaultModelHandle().lock();
        if (!model_handle) {
            impl_->last_error = "Model context unavailable";
            impl_->generate_status = failed;
            return build_response_json("", prompt, false, true);
        }
        
        json request_data = BuildRequestDataFromPrompt(prompt, true);
        
        model_handle->SetParamsByConfig(request_data);
        
        bool is_tool = false;
        impl_->input_builder = std::make_unique<ModelInputBuilder>(*impl_->chat_history,  impl_->model_manager->GetDefaultInstanceConfig(), nullptr);
        auto& model_input = impl_->input_builder->Build(request_data, is_tool);
        
        httplib::Request dummy_req;
        impl_->response_dispatcher->Prepare(model_input, is_tool, true, dummy_req, true);
        
        httplib::DataSink sink;
        sink.write = [callback](const char* data, size_t data_len) -> bool {
            return callback(std::string(data, data_len));
        };
        sink.is_writable = []() -> bool { return true; };
        sink.done = []() {};
        
        impl_->response_dispatcher->SendResponse(0, &sink, nullptr);
        
        impl_->generate_status = completed;
        status = loaded;
        impl_->current_status = loaded;
        
        return build_response_json("", prompt, false, true);
        
    } catch (const std::exception& e) {
        impl_->last_error = e.what();
        impl_->generate_status = failed;
        status = loaded;
        impl_->current_status = loaded;
        return build_response_json("", prompt, false, true);
    }
}

void api_interface::api_Reset() {
    if (!impl_ || !impl_->chat_history) {
        return;
    }
    
    try {
        std::lock_guard<std::mutex> lock(impl_->inference_mutex);
        impl_->chat_history->Clear();
        status = loaded;
        impl_->current_status = loaded;
        My_Log{My_Log::Level::kInfo} << "Chat history cleared" << std::endl;
    } catch (const std::exception& e) {
        impl_->last_error = e.what();
    }
}

int api_interface::api_token_num(const std::string& promptJson) {
    // 暂未实现：token 计数，保留导出签名以保证 ABI 兼容
    return 0;
}

bool api_interface::api_loadmodel(std::vector<uint8_t*>& buffers, std::vector<size_t>& buffersSize, const std::string& hwinfo) {
    // 暂未实现：从内存缓冲区加载模型
    return false;
}

bool api_interface::api_loadtoken(const std::string& token_fullpath) {
    // 暂未实现：单独加载 tokenizer
    return false;
}

bool api_interface::api_warmUp() {
    // 暂未实现：模型预热
    return false;
}

std::string api_interface::api_param_return() {
    // 暂未实现：返回当前推理参数
    return "{}";
}

bool api_interface::api_unloadtocpu() {
    // 暂未实现：卸载到 CPU
    return false;
}

void api_interface::api_StartLoop() {
    // 暂未实现：启动常驻循环
}

void api_interface::api_StopLoop() {
    // 暂未实现：停止常驻循环
}

// Private methods

std::string api_interface::api_performance_statistic() {
    // 暂未实现：性能统计
    return "{}";
}

std::string api_interface::build_response_json(const std::string& response, const std::string& prompt, bool is_stream, bool is_stream_end) {
    json result;
    result["response"] = response;
    result["prompt"] = prompt;
    result["is_stream"] = is_stream;
    result["is_stream_end"] = is_stream_end;
    result["status"] = impl_ ? impl_->generate_status : failed;
    return result.dump();
}

// Async streaming API implementations

bool api_interface::api_Generate_stream(const std::string& prompt) {
    if (!impl_) {
        return false;
    }
    
    impl_->current_prompt = prompt;
    impl_->generated_token.clear();
    impl_->stop_requested = false;
    
    // Start inference in a separate thread
    impl_->inference_thread_ = std::make_unique<std::thread>(&api_interface::inference_thread, this);
    
    return true;
}

std::string api_interface::api_Get_Generate_token() {
    if (!impl_) {
        return "";
    }
    
    std::lock_guard<std::mutex> lock(impl_->mutex);
    std::string token = impl_->generated_token;
    impl_->generated_token.clear();
    return token;
}

int api_interface::api_generate_status() {
    if (!impl_) {
        return failed;
    }
    return impl_->generate_status;
}

bool api_interface::api_stop() {
    if (!impl_) {
        return false;
    }
    
    impl_->stop_requested = true;
    impl_->generate_status = stopped;
    
    if (impl_->inference_thread_ && impl_->inference_thread_->joinable()) {
        impl_->inference_thread_->join();
    }
    
    return true;
}

void api_interface::stream_ask(const std::string& prompt) {
    // This is called by inference_thread
    api_Generate(prompt, [this](const std::string& chunk) -> bool {
        if (impl_->stop_requested) {
            return false;
        }
        
        // Append chunk to generated_token buffer
        // The buffer will be read by api_Get_Generate_token() and cleared
        {
            std::lock_guard<std::mutex> lock(impl_->mutex);
            impl_->generated_token += chunk;
        }
        return true;
    });
}

void api_interface::inference_thread() {
    impl_->generate_status = generating;
    stream_ask(impl_->current_prompt);
    
    if (impl_->stop_requested) {
        impl_->generate_status = stopped;
    } else {
        impl_->generate_status = completed;
    }
}
