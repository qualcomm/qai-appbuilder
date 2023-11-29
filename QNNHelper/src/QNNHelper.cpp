//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>

#include "../libQNNHelper/src/libQNNHelper.hpp"

namespace py = pybind11;

LibQNNHelper g_LibQNNHelper;

/*
    QNN_LOG_LEVEL_ERROR = 1,
    QNN_LOG_LEVEL_WARN = 2,
    QNN_LOG_LEVEL_INFO = 3,
    QNN_LOG_LEVEL_VERBOSE = 4,
    QNN_LOG_LEVEL_DEBUG = 5,
*/
int set_log_level(int32_t log_level) {
    return SetLogLevel(log_level);
}

int initialize(std::string model_name,
               std::string model_path, std::string backend_lib_path, std::string system_lib_path,
               std::string backend_ext_lib_path, std::string backend_ext_config_path) {
    return g_LibQNNHelper.ModelInitialize(model_name, model_path,
        backend_lib_path, system_lib_path, backend_ext_lib_path, backend_ext_config_path);
}

int initialize_P(std::string model_name, std::string proc_name,
                 std::string model_path, std::string backend_lib_path, std::string system_lib_path,
                 std::string backend_ext_lib_path, std::string backend_ext_config_path) {
    return g_LibQNNHelper.ModelInitialize(model_name, proc_name, model_path,
                                          backend_lib_path, system_lib_path, backend_ext_lib_path, backend_ext_config_path);
}

int destroy(std::string model_name) {
    return g_LibQNNHelper.ModelDestroy(model_name);
}

int destroy_P(std::string model_name, std::string proc_name) {
    return g_LibQNNHelper.ModelDestroy(model_name, proc_name);
}

int create_memory(std::string share_memory_name, size_t share_memory_size) {
    return g_LibQNNHelper.CreateShareMemory(share_memory_name, share_memory_size);
}

int delete_memory(std::string share_memory_name) {
    return g_LibQNNHelper.DeleteShareMemory(share_memory_name);
}

std::vector<py::array_t<float>> inference(std::string model_name, const std::vector<py::array_t<float>>& input) {
    std::vector<uint8_t*> inputBuffers;
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;

    //QNN_INF("inference input vector length: %d\n", input.size());

    for (auto i = 0; i < input.size(); i++) {
        py::buffer_info buf = input[i].request();
        inputBuffers.push_back(reinterpret_cast<uint8_t*>(buf.ptr));
    }

    g_LibQNNHelper.ModelInference(model_name, inputBuffers, outputBuffers, outputSize);

    //QNN_INF("inference::inference output vector length: %d\n", outputBuffers.size());

    std::vector<py::array_t<float>> output;  // TODO: convert outputBuffers to py:array_t...
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
                                            const std::vector<py::array_t<float>>& input) {
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

    g_LibQNNHelper.ModelInference(model_name, proc_name, share_memory_name, inputBuffers, inputSize, outputBuffers, outputSize);

    //QNN_INF("inference_P::inference output vector length: %d\n", outputBuffers.size());

    std::vector<py::array_t<float>> output;  // TODO: convert outputBuffers to py:array_t...
    //start_time();
    for (auto i = 0; i < outputBuffers.size(); i++) {
        size_t size = outputSize[i] / (sizeof(float) / sizeof(uint8_t));

        // https://github.com/pybind/pybind11/issues/1042#issuecomment-325941022
        // Avoid memory copy for saving time. 'py::capsule' for freeing the memory.
        py::capsule free_data((float*)outputBuffers[i], [](void* f) {});  // Not free this memory since it's share memory.
        auto result = py::array_t<float>(size, (float*)outputBuffers[i], free_data);

        /*
        py::buffer_info buf = result.request();
        float* ptr = (float*)buf.ptr;
        memcpy(ptr, (float*)outputBuffers[i], outputSize[i]);
        free(outputBuffers[i]);
        */

        output.push_back(result);
    }
    //print_time("convert Data To ArrayV");

    return output;
}


PYBIND11_MODULE(qnnhelper, m) {
m.doc() = R"pbdoc(
    Pybind11 QNNHelper plugin.
    -----------------------
    .. currentmodule:: qnnhelper
    .. autosummary::
        :toctree: _generate

        model_initialize
        model_inference
        model_destroy
        set_log_level
        )pbdoc";

m.def("model_initialize", &initialize, "Initialize models.");
m.def("model_initialize", &initialize_P, "Initialize models.");
m.def("model_inference", &inference, "Inference models.");
m.def("model_inference", &inference_P, "Inference models.");
m.def("model_destroy", &destroy, "Destroy models.");
m.def("model_destroy", &destroy_P, "Destroy models.");
m.def("memory_create", &create_memory, "Create share memory.");
m.def("memory_delete", &delete_memory, "Delete share memory.");
m.def("set_log_level", &set_log_level, "Set QNN log level.");
}
