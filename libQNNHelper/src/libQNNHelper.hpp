//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
#pragma once

#include <iostream>
#include <memory>
#include <string>
#include <vector>
#include<chrono>

#define LIBQNNHELPER_API __declspec(dllexport)


/////////////////////////////////////////////////////////////////////////////
/// Sync log time with 'SvcQNNHelper.exe' processes. For QNNHelper library internal use.
/////////////////////////////////////////////////////////////////////////////
extern "C" LIBQNNHELPER_API void SetProcInfo(std::string proc_name, uint64_t epoch);


/////////////////////////////////////////////////////////////////////////////
/// Log print.
/////////////////////////////////////////////////////////////////////////////
extern "C" LIBQNNHELPER_API void QNN_ERR(const char* fmt, ...);
extern "C" LIBQNNHELPER_API void QNN_WAR(const char* fmt, ...);
extern "C" LIBQNNHELPER_API void QNN_INF(const char* fmt, ...);
extern "C" LIBQNNHELPER_API void QNN_VEB(const char* fmt, ...);
extern "C" LIBQNNHELPER_API void QNN_DBG(const char* fmt, ...);
extern "C" LIBQNNHELPER_API bool SetLogLevel(int32_t log_level);


/////////////////////////////////////////////////////////////////////////////
/// Class libQNNHelper declaration.
/////////////////////////////////////////////////////////////////////////////
class  LIBQNNHELPER_API LibQNNHelper
{
public:
    bool ModelInitialize(std::string model_name, std::string model_path,
                         std::string backend_lib_path, std::string system_lib_path,
                         std::string backend_ext_lib_path, std::string backend_ext_config_path);
    bool ModelInitialize(std::string model_name, std::string proc_name, std::string model_path,
                         std::string backend_lib_path, std::string system_lib_path,
                         std::string backend_ext_lib_path, std::string backend_ext_config_path);

    bool ModelInference(std::string model_name, 
                        std::vector<uint8_t*>& inputBuffers, 
                        std::vector<uint8_t*>& outputBuffers, std::vector<size_t>& outputSize);
    bool ModelInference(std::string model_name, std::string proc_name, std::string share_memory_name,
                        std::vector<uint8_t*>& inputBuffers, std::vector<size_t>& inputSize,
                        std::vector<uint8_t*>& outputBuffers, std::vector<size_t>& outputSize);

    bool ModelDestroy(std::string model_name);
    bool ModelDestroy(std::string model_name, std::string proc_name);

    bool CreateShareMemory(std::string share_memory_name, size_t share_memory_size);
    bool DeleteShareMemory(std::string share_memory_name);
};


/////////////////////////////////////////////////////////////////////////////
/// Class TimerHelper declaration.
/////////////////////////////////////////////////////////////////////////////
#pragma warning(disable:4251)
class  LIBQNNHELPER_API TimerHelper
{
public:
    TimerHelper() {
        Reset();
    }

    void Reset() {
        time_start = std::chrono::steady_clock::now();
    }

    void Print(std::string message) {
        time_now = std::chrono::steady_clock::now();
        double dr_ms = std::chrono::duration<double, std::milli>(time_now - time_start).count();
        QNN_WAR("Time: %s %.2f\n", message.c_str(), dr_ms);
    }

    void Print(std::string message, bool reset) {
        Print(message);
        if (reset) {
            Reset();
        }
    }

private:
    std::chrono::steady_clock::time_point time_start;
    std::chrono::steady_clock::time_point time_now;
};
