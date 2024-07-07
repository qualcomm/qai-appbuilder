//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#ifndef _APPBUILDER_H
#define _APPBUILDER_H

#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>

#include "LibAppBuilder.hpp"

using namespace std;
namespace py = pybind11;

LibAppBuilder g_LibAppBuilder;

/*
    QNN_LOG_LEVEL_ERROR = 1,
    QNN_LOG_LEVEL_WARN = 2,
    QNN_LOG_LEVEL_INFO = 3,
    QNN_LOG_LEVEL_VERBOSE = 4,
    QNN_LOG_LEVEL_DEBUG = 5,
*/
int set_log_level(int32_t log_level, std::string log_path = "None") {
    return SetLogLevel(log_level, log_path);
}

/*
    OFF = 0,
    BASIC = 1,
    DETAILED = 2,
    INVALID = 3,
*/
int set_profiling_level(int32_t log_level) {
    return SetProfilingLevel(log_level);
}

int set_perf_profile(const std::string& perf_profile) {
    return SetPerfProfileGlobal(perf_profile);
}

int rel_perf_profile() {
    return RelPerfProfileGlobal();
}

int initialize(const std::string& model_name,
               const std::string& model_path, const std::string& backend_lib_path, const std::string& system_lib_path) {
    return g_LibAppBuilder.ModelInitialize(model_name, model_path, backend_lib_path, system_lib_path);
}

int initialize_P(const std::string& model_name, const std::string& proc_name,
                 const std::string& model_path, const std::string& backend_lib_path, const std::string& system_lib_path) {
    return g_LibAppBuilder.ModelInitialize(model_name, proc_name, model_path, backend_lib_path, system_lib_path);
}

int destroy(std::string model_name) {
    return g_LibAppBuilder.ModelDestroy(model_name);
}

int destroy_P(std::string model_name, std::string proc_name) {
    return g_LibAppBuilder.ModelDestroy(model_name, proc_name);
}

std::vector<py::array_t<float>> inference(std::string model_name, const std::vector<py::array_t<float>>& input, std::string perf_profile) {
    std::vector<uint8_t*> inputBuffers;
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;

    //QNN_INF("inference input vector length: %d\n", input.size());

    for (auto i = 0; i < input.size(); i++) {
        py::buffer_info buf = input[i].request();
        inputBuffers.push_back(reinterpret_cast<uint8_t*>(buf.ptr));
    }

    g_LibAppBuilder.ModelInference(model_name, inputBuffers, outputBuffers, outputSize, perf_profile);

    //QNN_INF("inference::inference output vector length: %d\n", outputBuffers.size());

    std::vector<py::array_t<float>> output;
    //start_time();
    for (auto i = 0; i < outputBuffers.size(); i++) {
        size_t size = outputSize[i] / (sizeof(float) / sizeof(uint8_t));

        // https://github.com/pybind/pybind11/issues/1042#issuecomment-325941022
        // Avoid memory copy for saving time. 'py::capsule' for freeing the memory.
        py::capsule free_data((float*)outputBuffers[i], [](void* f) {free(f);});
        auto result = py::array_t<float>(size, (float*)outputBuffers[i], free_data);

        output.push_back(result);
    }
    //print_time("convert Data To ArrayV");

    return output;
}

std::vector<py::array_t<float>> inference_P(std::string model_name, std::string proc_name, std::string share_memory_name,
                                            const std::vector<py::array_t<float>>& input, std::string perf_profile) {
    std::vector<uint8_t*> inputBuffers;
    std::vector<size_t> inputSize;
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;

    for (auto i = 0; i < input.size(); i++) {
        py::buffer_info buf = input[i].request();
        inputBuffers.push_back(reinterpret_cast<uint8_t*>(buf.ptr));
        size_t size = buf.size * (sizeof(float) / sizeof(uint8_t));
        inputSize.push_back(size);
        // QNN_INF("inference input data size: %llu\n", size);
    }

    g_LibAppBuilder.ModelInference(model_name, proc_name, share_memory_name, inputBuffers, inputSize, outputBuffers, outputSize, perf_profile);

    //QNN_INF("inference_P::inference output vector length: %d\n", outputBuffers.size());

    std::vector<py::array_t<float>> output;
    //start_time();
    for (auto i = 0; i < outputBuffers.size(); i++) {
        size_t size = outputSize[i] / (sizeof(float) / sizeof(uint8_t));

        // https://github.com/pybind/pybind11/issues/1042#issuecomment-325941022
        // Avoid memory copy for saving time. 'py::capsule' for freeing the memory.
        py::capsule free_data((float*)outputBuffers[i], [](void* f) {});  // Not free this memory since it's share memory.
        auto result = py::array_t<float>(size, (float*)outputBuffers[i], free_data);

        output.push_back(result);
    }
    //print_time("convert Data To ArrayV");

    return output;
}

int create_memory(std::string share_memory_name, size_t share_memory_size) {
    return g_LibAppBuilder.CreateShareMemory(share_memory_name, share_memory_size);
}

int delete_memory(std::string share_memory_name) {
    return g_LibAppBuilder.DeleteShareMemory(share_memory_name);
}

class ShareMemory {
public:
    std::string m_share_memory_name;

    ShareMemory(const std::string& share_memory_name, const size_t share_memory_size);
    ~ShareMemory();

};

class QNNContext {
public:
    std::string m_model_name;
    std::string m_proc_name;

    QNNContext(const std::string& model_name,
       	       const std::string& model_path, const std::string& backend_lib_path, const std::string& system_lib_path);

    QNNContext(const std::string& model_name, const std::string& proc_name,
       	       const std::string& model_path, const std::string& backend_lib_path, const std::string& system_lib_path);

    std::vector<py::array_t<float>> Inference(const std::vector<py::array_t<float>>& input, const std::string& perf_profile = "default");
    std::vector<py::array_t<float>> Inference(const ShareMemory& share_memory, const std::vector<py::array_t<float>>& input, const std::string& perf_profile = "default");

    ~QNNContext();
};

#endif

