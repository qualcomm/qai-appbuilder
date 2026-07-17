//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef LIB_MODEl_INTERFACE_HPP
#define LIB_MODEl_INTERFACE_HPP

#include <vector>
#include <functional>
#include <string>


#ifdef _WIN32
#ifdef GENIEAPI_EXPORTS
#define LIB_MODEl_INTERFACE_API __declspec(dllexport)
#else
#define LIB_MODEl_INTERFACE_API __declspec(dllimport)
#endif
#else
#define LIB_MODEl_INTERFACE_API
#endif

enum generater_status {
    completed = 0,    // finished generate 
    generating = 1,    // Running generation
    stopped = 2,    // generation stopped
    exceed = 3,    // exceed context size
    failed = -1    // generation failed
};

enum status
{
    init = 0,    // Init parameters
    loading = 1,   // Model is loading
    loaded = 2,    // Model loaded or reset
    unloaded = 3,    // Unload model -> Release model and tokenizer
    inference = 4,    // Running generation
    error = -1    // General error
};

enum hardware_type
{
    HW_GPU = 0,
    HW_IGPU = 1,
    HW_AGPU = 2,
    HW_ANPU = 3,
    HW_INPU = 4,
    HW_DNPU = 5
};

struct hardware_info
{
    int layer;
    int type;
};

enum Level
{
    kAlways,
    kError,
    kWarning,
    kInfo,
    kDebug,
    kVerbose,
};

class QInterfaceImpl;
class LIB_MODEl_INTERFACE_API api_interface {
public:
    api_interface(std::string& original, Level level = (Level)-1);

    ~api_interface();

    bool api_loadmodel(const std::string& model_path, std::vector<std::string>& model_name, const std::string& hwinfo);

    // Async model loading: starts loading in background thread, returns immediately
    bool api_loadmodel_async(const std::string& model_path, std::vector<std::string>& model_name, const std::string& hwinfo);

    // Wait for async model loading to complete, returns true if loaded successfully
    bool api_wait_loaded(int timeout_ms = -1);

    bool api_unloadmodel();

    int api_status();

    std::string api_Generate(const std::string& prompt);

    // stream answer
    std::string api_Generate(const std::string& prompt, std::function<bool(const std::string& chunk)> callback);

    // Async streaming APIs
    bool api_Generate_stream(const std::string& prompt);

    std::string api_Get_Generate_token();

    int api_generate_status();

    bool api_stop();

    void api_Reset();

    int api_token_num(const std::string& promptJson);

    // 暂未实现：从内存缓冲区加载模型
    bool api_loadmodel(std::vector<uint8_t*>& buffers, std::vector<size_t>& buffersSize, const std::string& hwinfo);

    // 暂未实现：单独加载 tokenizer
    bool api_loadtoken(const std::string& token_fullpath);

    // 暂未实现：模型预热
    bool api_warmUp();

    // 暂未实现：返回当前推理参数
    std::string api_param_return();

    // 暂未实现：卸载到 CPU
    bool api_unloadtocpu();

    // 暂未实现：启动常驻循环
    void api_StartLoop();

    // 暂未实现：停止常驻循环
    void api_StopLoop();

private:
    std::string api_performance_statistic();

    void stream_ask(const std::string& prompt);
    void inference_thread();
    std::string build_response_json(const std::string& response, const std::string& prompt, bool is_stream=false, bool is_stream_end=false);

    Level level_{};
    int status{ init };
    QInterfaceImpl* impl_{};
    std::string config_;
};

#endif // LIB_MODEl_INTERFACE_HPP
