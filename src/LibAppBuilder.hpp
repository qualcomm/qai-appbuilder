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
#include <chrono>
#include "Lora.hpp"

#ifdef _WIN32
    #ifdef DLL_EXPORTS
        #define LIBAPPBUILDER_API __declspec(dllexport)
    #else
        #define LIBAPPBUILDER_API __declspec(dllimport)
    #endif
#else // _WIN32
    #define LIBAPPBUILDER_API
#endif

/////////////////////////////////////////////////////////////////////////////
/// Sync log time with 'QAIAppSvc.exe' processes. For AppBuilder library internal use.
/////////////////////////////////////////////////////////////////////////////
extern "C" LIBAPPBUILDER_API void SetProcInfo(std::string proc_name, uint64_t epoch);


/////////////////////////////////////////////////////////////////////////////
/// Log print.
/////////////////////////////////////////////////////////////////////////////
extern "C" LIBAPPBUILDER_API void QNN_ERR(const char* fmt, ...);
extern "C" LIBAPPBUILDER_API void QNN_WAR(const char* fmt, ...);
extern "C" LIBAPPBUILDER_API void QNN_INF(const char* fmt, ...);
extern "C" LIBAPPBUILDER_API void QNN_VEB(const char* fmt, ...);
extern "C" LIBAPPBUILDER_API void QNN_DBG(const char* fmt, ...);
extern "C" LIBAPPBUILDER_API bool SetLogLevel(int32_t log_level, const std::string log_path = "None");
extern "C" LIBAPPBUILDER_API bool SetProfilingLevel(int32_t profiling_level);
extern "C" LIBAPPBUILDER_API bool SetPerfProfileGlobal(const std::string& perf_profile);
extern "C" LIBAPPBUILDER_API bool RelPerfProfileGlobal();


/////////////////////////////////////////////////////////////////////////////
/// Class LibAppBuilder declaration.
/////////////////////////////////////////////////////////////////////////////
class LIBAPPBUILDER_API LibAppBuilder
{
public:
    bool ModelInitialize(const std::string& model_name, const std::string& model_path,
                               const std::string& backend_lib_path, const std::string& system_lib_path);
    bool ModelInitialize(const std::string& model_name, const std::string& proc_name, const std::string& model_path,
                               const std::string& backend_lib_path, const std::string& system_lib_path);

    bool ModelInitialize(const std::string& model_name, const std::string& model_path,
                         const std::string& backend_lib_path, const std::string& system_lib_path, const std::vector<LoraAdapter>& lora_adapters);

    bool ModelInference(std::string model_name, std::vector<uint8_t*>& inputBuffers, 
                              std::vector<uint8_t*>& outputBuffers, std::vector<size_t>& outputSize,
                              std::string& perfProfile);
    bool ModelInference(std::string model_name, std::string proc_name, std::string share_memory_name,
                              std::vector<uint8_t*>& inputBuffers, std::vector<size_t>& inputSize,
                              std::vector<uint8_t*>& outputBuffers, std::vector<size_t>& outputSize,
                              std::string& perfProfile);

    bool ModelDestroy(std::string model_name);
    bool ModelDestroy(std::string model_name, std::string proc_name);

    bool CreateShareMemory(std::string share_memory_name, size_t share_memory_size);
    bool DeleteShareMemory(std::string share_memory_name);
};


/////////////////////////////////////////////////////////////////////////////
/// Class TimerHelper declaration.
/////////////////////////////////////////////////////////////////////////////
#pragma warning(disable:4251)
class LIBAPPBUILDER_API TimerHelper
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

