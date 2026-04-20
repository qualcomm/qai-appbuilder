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
#include "log.h"
#include "utils.h"
#include <thread>
#include <mutex>
#include <sstream>
#include <ctime>
#include <filesystem>
#include <GenieCommon.h>

#ifdef WIN32
#include <direct.h>
#else
#include <unistd.h>
#endif

namespace fs = std::filesystem;

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
    bool initialized = false;
    bool service_running = false;
    bool model_loaded = false;
    bool is_generating = false;
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
    
    bool InitializeModelManager(const std::string& config_json) {
        try {
            // Parse config JSON string and save to temporary file
            json config_data = json::parse(config_json);
            
            // Extract dialog config
            if (!config_data.contains("dialog")) {
                last_error = "Config JSON must contain 'dialog' section";
                return false;
            }
            
            // Create a temporary config file in memory or use the JSON directly
            // For now, we'll store the JSON and pass it to model manager
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
            
            // Create model manager
            model_manager = std::make_unique<ModelManager>(std::move(model_config));
            
            // Don't call InitializeConfig since we're using JSON string
            // We'll set the necessary fields directly
            model_manager->config_file_ = config_file;
            
            // Create chat history
            chat_history = std::make_unique<ChatHistory>(*model_manager);
            
            // Create response dispatcher
            response_dispatcher = std::make_unique<ResponseDispatcher>(*model_manager, *chat_history);
            
            // Create input builder
            input_builder = std::make_unique<ModelInputBuilder>(*chat_history, *model_manager);
            
            return true;
            
        } catch (const std::exception& e) {
            last_error = std::string("Model manager initialization failed: ") + e.what();
            return false;
        }
    }
};

// api_interface implementation

api_interface::api_interface(std::string& config) : config_(config), impl_(nullptr), status(init) {
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
        
        if (!impl_->InitializeModelManager(config_)) {
            printf("[ERROR] api_loadmodel: InitializeModelManager failed, error: %s\n", impl_->last_error.c_str());
            fflush(stdout);
            status = error;
            impl_->current_status = error;
            return false;
        }
    }
    
    try {
        std::lock_guard<std::mutex> lock(impl_->inference_mutex);
        status = loading;
        impl_->current_status = loading;
        
        // Check if we're using JSON string config (config_file_ starts with '{')
        std::string trimmed = impl_->model_manager->config_file_;
        size_t start = trimmed.find_first_not_of(" \t\n\r");
        if (start != std::string::npos) {
            trimmed = trimmed.substr(start);
        }
        
        bool is_json_config = (!trimmed.empty() && trimmed[0] == '{');
        
        if (is_json_config) {
            // Using JSON string config - set model path and call LoadModel directly
            // Set the actual model path from the parameter
            impl_->model_manager->model_path_ = model_path;
            impl_->model_manager->model_name_ = "json_config_model";
            impl_->model_manager->model_root_ = ".";
            // Keep config_file_ as JSON string - don't change it!
            // impl_->model_manager->config_file_ is already the JSON string from constructor
            
            if (!impl_->model_manager->LoadModel()) {
                impl_->last_error = "Failed to load model";
                printf("[ERROR] api_loadmodel: LoadModel failed\n");
                fflush(stdout);
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
                printf("[ERROR] api_loadmodel: LoadModelByName failed\n");
                fflush(stdout);
                status = error;
                impl_->current_status = error;
                return false;
            }
            
            if (first_load && impl_->response_dispatcher) {
                impl_->response_dispatcher->ResetProcessor();
            }
        }
        
        impl_->model_loaded = true;
        status = loaded;
        impl_->current_status = loaded;
        return true;
        
    } catch (const std::exception& e) {
        impl_->last_error = e.what();
        printf("[ERROR] api_loadmodel: Exception caught: %s\n", e.what());
        fflush(stdout);
        status = error;
        impl_->current_status = error;
        return false;
    }
}

bool api_interface::api_loadmodel_async(const std::string& model_path, std::vector<std::string>& model_name, const std::string& hwinfo) {
    if (!impl_) {
        impl_ = new QInterfaceImpl();
        if (!impl_->InitializeModelManager(config_)) {
            printf("[ERROR] api_loadmodel_async: InitializeModelManager failed\n");
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
                impl_->model_loaded = true;
                this->status = ::loaded;
                impl_->current_status = ::loaded;
            } else {
                impl_->last_error = "Failed to load model: " + mp;
                this->status = ::error;
                impl_->current_status = ::error;
                printf("[ERROR] api_loadmodel_async: LoadModelByName failed\n");
            }
        } catch (const std::exception& e) {
            printf("[ERROR] api_loadmodel_async: Exception caught: %s\n", e.what());
            printf("[ERROR] api_loadmodel_async: Exception details: %s\n", e.what());
            fflush(stdout);
            impl_->last_error = std::string("Exception in model load: ") + e.what();
            this->status = ::error;
            impl_->current_status = ::error;
        } catch (...) {
            printf("[ERROR] api_loadmodel_async: Unknown exception caught!\n");
            fflush(stdout);
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
                printf("[INFO] api_wait_loaded: Still loading model, please wait...\n");
                fflush(stdout);
            }
        }
    } else {
        impl_->load_cv.wait_for(lk, std::chrono::milliseconds(timeout_ms), pred);
    }

    if (!impl_->load_done) {
        printf("[WARN] api_wait_loaded: Timed out waiting for model load\n");
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
        
        auto model_handle = impl_->model_manager->get_genie_model_handle().lock();
        if (!model_handle) {
            impl_->last_error = "Model context unavailable";
            impl_->generate_status = failed;
            return build_response_json("", prompt, false, false);
        }
        
        // Build JSON request
        json request_data;
        
        // Try to parse prompt as JSON first
        bool parsed_json = false;
        try {
            request_data = json::parse(prompt);
            parsed_json = true;
        } catch (...) {
            // Prompt is not JSON, will build request from plain text
        }
        
        // If not JSON, build request from plain text
        if (!parsed_json) {
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
        
        request_data["stream"] = false;
        
        // Set parameters
        model_handle->SetParamsByConfig(request_data);
        
        // Build model input
        bool is_tool = false;
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
        
        auto model_handle = impl_->model_manager->get_genie_model_handle().lock();
        if (!model_handle) {
            impl_->last_error = "Model context unavailable";
            impl_->generate_status = failed;
            return build_response_json("", prompt, false, true);
        }
        
        // Build JSON request
        json request_data;
        
        // Try to parse prompt as JSON first
        bool parsed_json = false;
        try {
            request_data = json::parse(prompt);
            parsed_json = true;
        } catch (...) {
            // Prompt is not JSON, will build request from plain text
        }
        
        // If not JSON, build request from plain text
        if (!parsed_json) {
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
        
        request_data["stream"] = true;
        
        model_handle->SetParamsByConfig(request_data);
        
        bool is_tool = false;
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
    // TODO: Implement token counting
    return 0;
}

bool api_interface::api_loadmodel(std::vector<uint8_t*>& buffers, std::vector<size_t>& buffersSize, const std::string& hwinfo) {
    // TODO: Implement loading from memory buffers
    return false;
}

bool api_interface::api_loadtoken(const std::string& token_fullpath) {
    // TODO: Implement tokenizer loading
    return false;
}

bool api_interface::api_warmUp() {
    // TODO: Implement warm-up
    return false;
}

std::string api_interface::api_param_return() {
    // TODO: Implement parameter return
    return "{}";
}

bool api_interface::api_unloadtocpu() {
    // TODO: Implement unload to CPU
    return false;
}

void api_interface::api_StartLoop() {
    // TODO: Implement start loop
}

void api_interface::api_StopLoop() {
    // TODO: Implement stop loop
}

// Private methods

std::string api_interface::api_performance_statistic() {
    // TODO: Implement performance statistics
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
