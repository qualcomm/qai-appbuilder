//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef LIB_MODEl_INTERFACE_HPP
#define LIB_MODEl_INTERFACE_HPP

#include <iostream>
#include <memory>
#include <vector>
#include <map>
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

typedef int32_t int32;
typedef float float32;

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
    int32 layer;
    int32 type;
};

class QInterfaceImpl;
class LIB_MODEl_INTERFACE_API api_interface {
public:
    api_interface(std::string& original);

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

    // TODO
    bool api_loadmodel(std::vector<uint8_t*>& buffers, std::vector<size_t>& buffersSize, const std::string& hwinfo);

    // TODO
    bool api_loadtoken(const std::string& token_fullpath);

    // TODO
    bool api_warmUp();

    // TODO
    std::string api_param_return();

    // TODO
    bool api_unloadtocpu();

    // TODO
    void api_StartLoop();

    // TODO
    void api_StopLoop();

private:
    //bool update_params(const params& params);
    
    //get prompt token number
    std::string api_performance_statistic();

    void stream_ask(const std::string& prompt);
    void inference_thread();
    std::string build_response_json(const std::string& response, const std::string& prompt, bool is_stream=false, bool is_stream_end=false);

    int status{ init };
    int token_exceed = 0;
    bool canceled = false;
    QInterfaceImpl* impl_{};
    std::string config_;
    std::time_t generate_start_time_;
    //params _params;
};

#endif // LIB_MODEl_INTERFACE_HPP
