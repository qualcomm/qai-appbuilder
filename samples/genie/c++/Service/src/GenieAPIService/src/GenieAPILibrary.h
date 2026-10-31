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
#include <atomic>
#include <mutex>


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
    completed = 0,
    generating = 1,
    stopped = 2,
    exceed = 3,
    failed = -1
};

enum status
{
    init = 0,
    loading = 1,
    loaded = 2,
    unloaded = 3,
    inference = 4,
    error = -1
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

    bool api_loadmodel_async(const std::string& model_path, std::vector<std::string>& model_name, const std::string& hwinfo);

    bool api_wait_loaded(int timeout_ms = -1);

    bool api_unloadmodel();

    int api_status();

    std::string api_Generate(const std::string& prompt);

    std::string api_Generate(const std::string& prompt, std::function<bool(const std::string& chunk)> callback);

    bool api_Generate_stream(const std::string& prompt);

    std::string api_Get_Generate_token();

    int api_generate_status();

    bool api_stop();

    void api_Reset();

    int api_token_num(const std::string& promptJson);

    bool api_loadmodel(std::vector<uint8_t*>& buffers, std::vector<size_t>& buffersSize, const std::string& hwinfo);

    bool api_loadtoken(const std::string& token_fullpath);

    bool api_warmUp();

    std::string api_param_return();

    bool api_unloadtocpu();

    void api_StartLoop();

    void api_StopLoop();

private:
    std::string api_performance_statistic();

    void stream_ask(const std::string& prompt);
    void inference_thread();
    bool EnsureImpl(const char* tag);
    std::string build_response_json(const std::string& response, const std::string& prompt, bool is_stream=false, bool is_stream_end=false);

    Level level_{};
    std::atomic<int> status{ init };
    QInterfaceImpl* impl_{};
    std::mutex impl_mutex_;
    std::string config_;
};

#endif // LIB_MODEl_INTERFACE_HPP
