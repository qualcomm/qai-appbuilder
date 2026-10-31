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
#include <atomic>
#include <mutex>
#include <thread>

#ifdef WIN32
#include <direct.h>
#else
#include <unistd.h>
#endif

class QInterfaceImpl {
public:
    std::unique_ptr<std::thread> inference_thread_;
    std::unique_ptr<std::thread> load_thread_;
    std::mutex thread_mutex;
    std::atomic<bool> inference_running{false};
    std::mutex error_mutex;
    std::string last_error_;
    std::mutex mutex;
    std::mutex inference_mutex;
    std::mutex load_mutex;
    std::condition_variable load_cv;
    bool load_done = false;
    bool load_result = false;
    std::atomic<bool> model_loaded{false};
    std::atomic<bool> stop_requested{false};

    std::string current_prompt;
    std::string generated_token;
    std::function<bool(const std::string&)> stream_callback;

    std::unique_ptr<ModelManager> model_manager;
    std::unique_ptr<ChatHistory> chat_history;
    std::unique_ptr<ResponseDispatcher> response_dispatcher;
    std::unique_ptr<ModelInputBuilder> input_builder;

    std::string config_file;
    std::string lora_adapter;
    std::string log_file;
    int log_level = 2;
    int num_response = 10;
    int min_output_num = 1;
    float lora_alpha = 1.0f;
    bool output_all_text = false;
    bool enable_thinking = false;

    std::atomic<int> current_status{init};
    std::atomic<int> generate_status{completed};

    void SetError(std::string msg) {
        std::lock_guard<std::mutex> lock(error_mutex);
        last_error_ = std::move(msg);
    }

    std::string GetError() {
        std::lock_guard<std::mutex> lock(error_mutex);
        return last_error_;
    }

    bool InitializeModelManager(const std::string& config_json, Level level) {
        try {
            std::string trimmed = config_json;
            size_t start = trimmed.find_first_not_of(" \t\n\r");
            trimmed = (start != std::string::npos) ? trimmed.substr(start) : std::string();
            bool is_inline_json = (!trimmed.empty() && trimmed[0] == '{');

            if (is_inline_json) {
                json config_data = json::parse(config_json);
                if (!config_data.contains("dialog")) {
                    SetError("Config JSON must contain 'dialog' section");
                    return false;
                }
            } else if (!File::IsFileExist(config_json)) {
                SetError("config is neither an inline JSON object nor an existing config file path: " + config_json);
                return false;
            }

            config_file = config_json;

            IModelConfig model_config;
            model_config.config_file_ = config_file;
            model_config.loraAdapter = lora_adapter;
            model_config.loraAlpha = lora_alpha;
            model_config.num_response_ = num_response;
            model_config.minOutputNum = min_output_num;
            model_config.outputAllText = output_all_text;
            model_config.enableThinking = enable_thinking;
            model_config.log_level_ = level;

            model_manager = std::make_unique<ModelManager>(std::move(model_config));
            model_manager->config_file_ = config_file;

            chat_history = std::make_unique<ChatHistory>(*model_manager);
            response_dispatcher = std::make_unique<ResponseDispatcher>(*model_manager, *chat_history);

            return true;

        } catch (const std::exception& e) {
            SetError(std::string("Model manager initialization failed: ") + e.what());
            return false;
        }
    }

};

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

api_interface::api_interface(std::string& config, Level level) : config_(config), level_{level}
{
    static std::mutex dir_init_mutex;
    std::lock_guard<std::mutex> dir_lock(dir_init_mutex);
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
        RootDir = CurrentDir;
    }
}

api_interface::~api_interface() {
    if (impl_) {
        api_stop();
        std::unique_ptr<std::thread> load_thread;
        {
            std::lock_guard<std::mutex> lock(impl_->thread_mutex);
            load_thread = std::move(impl_->load_thread_);
        }
        if (load_thread && load_thread->joinable()) {
            load_thread->join();
        }
        delete impl_;
        impl_ = nullptr;
    }
}

bool api_interface::EnsureImpl(const char* tag) {
    std::lock_guard<std::mutex> lock(impl_mutex_);
    if (!impl_) {
        impl_ = new QInterfaceImpl();
    }
    if (impl_->model_manager) {
        return true;
    }
    if (!impl_->InitializeModelManager(config_, level_)) {
        My_Log{My_Log::Level::kError} << "[" << tag << "] InitializeModelManager failed: " << impl_->GetError() << std::endl;
        status = error;
        impl_->current_status = error;
        return false;
    }
    return true;
}

bool api_interface::api_loadmodel(const std::string& model_path, std::vector<std::string>& model_name, const std::string& hwinfo) {
    if (!EnsureImpl("api_loadmodel")) {
        return false;
    }

    try {
        std::lock_guard<std::mutex> lock(impl_->inference_mutex);
        status = loading;
        impl_->current_status = loading;

        std::string trimmed = impl_->model_manager->config_file_;
        size_t start = trimmed.find_first_not_of(" \t\n\r");
        if (start != std::string::npos) {
            trimmed = trimmed.substr(start);
        }

        bool is_direct_config = (!trimmed.empty() && trimmed[0] == '{') || File::IsFileExist(trimmed);

        if (is_direct_config) {
            impl_->model_manager->model_path_ = model_path;
            impl_->model_manager->model_name_ = std::filesystem::path(model_path).filename().generic_string();
            impl_->model_manager->model_root_ = std::filesystem::path(model_path).parent_path().generic_string();

            if (!impl_->model_manager->LoadSingleModel()) {
                impl_->SetError("Failed to load model");
                My_Log{My_Log::Level::kError} << "[api_loadmodel] LoadSingleModel failed" << std::endl;
                status = error;
                impl_->current_status = error;
                return false;
            }
            if (impl_->response_dispatcher) {
                impl_->response_dispatcher->ResetProcessor();
            }
        } else {
            bool first_load = false;
            if (!impl_->model_manager->LoadModelByName(model_path, first_load)) {
                impl_->SetError("Failed to load model: " + model_path);
                My_Log{My_Log::Level::kError} << "[api_loadmodel] LoadModelByName failed: " << model_path << std::endl;
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
        impl_->SetError(e.what());
        My_Log{My_Log::Level::kError} << "[api_loadmodel] Exception caught: " << e.what() << std::endl;
        status = error;
        impl_->current_status = error;
        return false;
    }
}

bool api_interface::api_loadmodel_async(const std::string& model_path, std::vector<std::string>& model_name, const std::string& hwinfo) {
    if (!EnsureImpl("api_loadmodel_async")) {
        return false;
    }

    std::lock_guard<std::mutex> thread_lock(impl_->thread_mutex);
    if (impl_->load_thread_) {
        bool previous_done = false;
        {
            std::lock_guard<std::mutex> lk(impl_->load_mutex);
            previous_done = impl_->load_done;
        }
        if (!previous_done) {
            My_Log{My_Log::Level::kWarning} << "[api_loadmodel_async] previous load is still running" << std::endl;
            return false;
        }
        if (impl_->load_thread_->joinable()) {
            impl_->load_thread_->join();
        }
        impl_->load_thread_.reset();
    }

    {
        std::lock_guard<std::mutex> lk(impl_->load_mutex);
        impl_->load_done = false;
        impl_->load_result = false;
    }

    status = loading;
    impl_->current_status = loading;

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
                impl_->SetError("Failed to load model: " + mp);
                this->status = ::error;
                impl_->current_status = ::error;
                My_Log{My_Log::Level::kError} << "[api_loadmodel_async] LoadModelByName failed: " << mp << std::endl;
            }
        } catch (const std::exception& e) {
            My_Log{My_Log::Level::kError} << "[api_loadmodel_async] Exception caught: " << e.what() << std::endl;
            impl_->SetError(std::string("Exception in model load: ") + e.what());
            this->status = ::error;
            impl_->current_status = ::error;
        } catch (...) {
            My_Log{My_Log::Level::kError} << "[api_loadmodel_async] Unknown exception caught" << std::endl;
            impl_->SetError("Unknown exception in model load");
            this->status = ::error;
            impl_->current_status = ::error;
        }

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

    const bool result = impl_->load_result;
    lk.unlock();

    std::unique_ptr<std::thread> load_thread;
    {
        std::lock_guard<std::mutex> lock(impl_->thread_mutex);
        load_thread = std::move(impl_->load_thread_);
    }
    if (load_thread && load_thread->joinable()) {
        load_thread->join();
    }

    return result;
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
        impl_->SetError(e.what());
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
            impl_->SetError("Model context unavailable");
            impl_->generate_status = failed;
            return build_response_json("", prompt, false, false);
        }

        json request_data = BuildRequestDataFromPrompt(prompt, false);

        model_handle->SetParamsByConfig(request_data);

        bool is_tool = false;
        impl_->input_builder = std::make_unique<ModelInputBuilder>(*impl_->chat_history,  impl_->model_manager->GetDefaultInstanceConfig(), nullptr);
        auto& model_input = impl_->input_builder->Build(request_data, is_tool);
        impl_->response_dispatcher->SetSkillMappings(impl_->input_builder->GetRuntimeSkillMappings());

        httplib::Request dummy_req;
        impl_->response_dispatcher->Prepare(model_input, is_tool, false, dummy_req, true);

        httplib::Response dummy_res;
        if (!impl_->response_dispatcher->SendResponse(0, nullptr, &dummy_res)) {
            impl_->SetError("Inference failed");
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
        impl_->SetError(e.what());
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
            impl_->SetError("Model context unavailable");
            impl_->generate_status = failed;
            return build_response_json("", prompt, false, true);
        }

        json request_data = BuildRequestDataFromPrompt(prompt, true);

        model_handle->SetParamsByConfig(request_data);

        bool is_tool = false;
        impl_->input_builder = std::make_unique<ModelInputBuilder>(*impl_->chat_history,  impl_->model_manager->GetDefaultInstanceConfig(), nullptr);
        auto& model_input = impl_->input_builder->Build(request_data, is_tool);
        impl_->response_dispatcher->SetSkillMappings(impl_->input_builder->GetRuntimeSkillMappings());

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
        impl_->SetError(e.what());
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
        impl_->SetError(e.what());
    }
}

int api_interface::api_token_num(const std::string& promptJson) {
    return 0;
}

bool api_interface::api_loadmodel(std::vector<uint8_t*>& buffers, std::vector<size_t>& buffersSize, const std::string& hwinfo) {
    return false;
}

bool api_interface::api_loadtoken(const std::string& token_fullpath) {
    return false;
}

bool api_interface::api_warmUp() {
    return false;
}

std::string api_interface::api_param_return() {
    return "{}";
}

bool api_interface::api_unloadtocpu() {
    return false;
}

void api_interface::api_StartLoop() {
}

void api_interface::api_StopLoop() {
}

std::string api_interface::api_performance_statistic() {
    return "{}";
}

std::string api_interface::build_response_json(const std::string& response, const std::string& prompt, bool is_stream, bool is_stream_end) {
    json result;
    result["response"] = response;
    result["prompt"] = prompt;
    result["is_stream"] = is_stream;
    result["is_stream_end"] = is_stream_end;
    result["status"] = impl_ ? impl_->generate_status.load() : static_cast<int>(failed);
    return result.dump();
}

bool api_interface::api_Generate_stream(const std::string& prompt) {
    if (!impl_) {
        return false;
    }

    std::lock_guard<std::mutex> thread_lock(impl_->thread_mutex);
    if (impl_->inference_running.exchange(true)) {
        My_Log{My_Log::Level::kWarning} << "[api_Generate_stream] previous generation is still running" << std::endl;
        return false;
    }
    if (impl_->inference_thread_) {
        if (impl_->inference_thread_->joinable()) {
            impl_->inference_thread_->join();
        }
        impl_->inference_thread_.reset();
    }

    impl_->current_prompt = prompt;
    {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        impl_->generated_token.clear();
    }
    impl_->stop_requested = false;

    try {
        impl_->inference_thread_ = std::make_unique<std::thread>(&api_interface::inference_thread, this);
    } catch (const std::exception& e) {
        impl_->inference_running = false;
        impl_->SetError(e.what());
        return false;
    }

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

    if (impl_->model_manager) {
        auto model_handle = impl_->model_manager->GetDefaultModelHandle().lock();
        if (model_handle) {
            model_handle->Stop();
        }
    }

    std::unique_ptr<std::thread> inference_thread;
    {
        std::lock_guard<std::mutex> lock(impl_->thread_mutex);
        if (impl_->inference_thread_ && impl_->inference_thread_->joinable() &&
            impl_->inference_thread_->get_id() != std::this_thread::get_id()) {
            inference_thread = std::move(impl_->inference_thread_);
        }
    }
    if (inference_thread) {
        inference_thread->join();
    }

    return true;
}

void api_interface::stream_ask(const std::string& prompt) {
    api_Generate(prompt, [this](const std::string& chunk) -> bool {
        if (impl_->stop_requested) {
            return false;
        }

        {
            std::lock_guard<std::mutex> lock(impl_->mutex);
            impl_->generated_token += chunk;
        }
        return true;
    });
}

void api_interface::inference_thread() {
    impl_->generate_status = generating;
    try {
        stream_ask(impl_->current_prompt);

        if (impl_->stop_requested) {
            impl_->generate_status = stopped;
        } else {
            impl_->generate_status = completed;
        }
    } catch (...) {
        impl_->generate_status = failed;
    }
    impl_->inference_running = false;
}
