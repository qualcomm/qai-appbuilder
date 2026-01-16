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
#include <cstdint>
#include <numeric>
#include <unordered_map>
#include <mutex>
#include <algorithm>
#include <cctype>

#include "LibAppBuilder.hpp"
#include "Lora.hpp"

using namespace std;
namespace py = pybind11;

LibAppBuilder g_LibAppBuilder;

// -----------------------------------------------------------------------------
// Helper: map dtype string (from getInputDataType/getOutputDataType) to numpy dtype
// -----------------------------------------------------------------------------
static inline py::dtype dtypeFromString(const std::string& dtypeStr) {
    std::string t = dtypeStr;
    for (auto& c : t) c = static_cast<char>(::tolower(c));

    if (t == "int8") {
        return py::dtype::of<int8_t>();
    } else if (t == "uint8") {
        return py::dtype::of<uint8_t>();
    } else if (t == "int16") {
        return py::dtype::of<int16_t>();
    } else if (t == "uint16") {
        return py::dtype::of<uint16_t>();
    } else if (t == "int32") {
        return py::dtype::of<int32_t>();
    } else if (t == "uint32") {
        return py::dtype::of<uint32_t>();
    } else if (t == "int64") {
        return py::dtype::of<int64_t>();
    } else if (t == "uint64") {
        return py::dtype::of<uint64_t>();
    } else if (t == "float16" || t == "fp16") {
        // NOTE:
        // Some pybind11 versions (including some builds with CPython 3.12) do NOT provide pybind11::half.
        // Use NumPy dtype string instead, which is stable across pybind11 versions.
        // This requires NumPy to be available at runtime (it already is for py::array usage).
        return py::dtype("float16");
    } else if (t == "float32" || t == "fp32" || t == "float") {
        return py::dtype::of<float>();
    } else if (t == "float64" || t == "fp64" || t == "double") {
        return py::dtype::of<double>();
    } else if (t == "bool" || t == "bool_" || t == "bool8") {
        return py::dtype::of<bool>();
    }

    // Fallback: treat as raw bytes
    return py::dtype::of<uint8_t>();
}

// ---------------------------------------------------------------------------
// Helper: product of dims (for output element count)
// ---------------------------------------------------------------------------
static inline size_t productDims(const std::vector<size_t>& dims) {
    if (dims.empty()) return 0;
    size_t p = 1;
    for (size_t d : dims) {
        // guard overflow is optional; keep simple
        p *= d;
    }
    return p;
}

// ---------------------------------------------------------------------------
// Helper: infer actual numpy dtype from (outputSizeBytes, outputShape, modelReportedDtype)
// - For output_data_type == float (FLOAT_ONLY), QnnSampleApp may convert outputs to float32 and
//   report outputSize as floatBytes, while getOutputDataType() still returns model native dtype.
// - We use bytesPerElem = outputSizeBytes / expectedElems to detect real buffer dtype.
// ---------------------------------------------------------------------------
static inline py::dtype inferOutputNumpyDtype(size_t outputSizeBytes,
                                             const std::vector<size_t>& outShape,
                                             const std::string& modelReportedDtypeStr) {
    // fallback to model-reported dtype
    py::dtype fallback = dtypeFromString(modelReportedDtypeStr);

    const size_t expectedElems = productDims(outShape);
    if (expectedElems == 0) {
        return fallback;
    }
    if (outputSizeBytes == 0) {
        return fallback;
    }
    if ((outputSizeBytes % expectedElems) != 0) {
        // size mismatch; keep old behavior
        return fallback;
    }

    const size_t bytesPerElem = outputSizeBytes / expectedElems;

    // Most important cases for float/native compatibility:
    // - float32 buffers (FLOAT_ONLY conversion): 4 bytes/elem
    // - float16 native buffers: 2 bytes/elem
    if (bytesPerElem == 4) {
        return py::dtype::of<float>();
    }
    if (bytesPerElem == 2) {
        // if model says float16/fp16, keep float16; otherwise still safe to expose float16 raw
        return py::dtype("float16");
    }
    if (bytesPerElem == 1) {
        // choose signedness based on model-reported type when possible
        std::string t = modelReportedDtypeStr;
        for (auto& c : t) c = static_cast<char>(::tolower(c));
        if (t == "int8")  return py::dtype::of<int8_t>();
        if (t == "bool" || t == "bool_" || t == "bool8") return py::dtype::of<bool>();
        return py::dtype::of<uint8_t>();
    }
    if (bytesPerElem == 8) {
        // could be int64/uint64/double; follow model dtype if it matches, else expose uint8 fallback
        std::string t = modelReportedDtypeStr;
        for (auto& c : t) c = static_cast<char>(::tolower(c));
        if (t == "float64" || t == "fp64" || t == "double") return py::dtype::of<double>();
        if (t == "int64")  return py::dtype::of<int64_t>();
        if (t == "uint64") return py::dtype::of<uint64_t>();
        return fallback;
    }
    // otherwise fallback
    return fallback;
}

ModelInfo_t getModelInfo_P(std::string model_name, std::string proc_name, 
                           std::string input, size_t graphIndex = 0) {

    ModelInfo_t output = g_LibAppBuilder.getModelInfo(model_name, proc_name, input);
    return output;
}


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
               const std::string& model_path, const std::string& backend_lib_path, const std::string& system_lib_path, 
               bool async, const std::string& input_data_type, const std::string& output_data_type) {
    return g_LibAppBuilder.ModelInitialize(model_name, model_path, backend_lib_path, system_lib_path, async, input_data_type, output_data_type);
}

int initialize_P(const std::string& model_name, const std::string& proc_name,
                 const std::string& model_path, const std::string& backend_lib_path, const std::string& system_lib_path, 
                 bool async, const std::string& input_data_type, const std::string& output_data_type) {
    return g_LibAppBuilder.ModelInitialize(model_name, proc_name, model_path, backend_lib_path, system_lib_path, async, input_data_type, output_data_type);
}

int destroy(std::string model_name) {
    return g_LibAppBuilder.ModelDestroy(model_name);
}

int destroy_P(std::string model_name, std::string proc_name) {
    return g_LibAppBuilder.ModelDestroy(model_name, proc_name);
}

std::vector<py::array> inference(std::string model_name, const std::vector<py::array>& input, 
                                 std::string perf_profile, size_t graphIndex = 0, 
                                 const std::string& input_data_type="float", const std::string& output_data_type="float") {
    std::vector<uint8_t*> inputBuffers;
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;

    // Keep temporary converted/contiguous arrays alive during ModelInference
    std::vector<py::array> keepAlive;
    const bool floatMode = (input_data_type == "float");

    //QNN_INF("inference input vector length: %d\n", input.size());

    for (auto i = 0; i < input.size(); i++) {
        if (floatMode) {
            // Backward compatible: force-cast to float32 contiguous like old py::array_t<float>
            py::array_t<float, py::array::c_style | py::array::forcecast> farr(input[i]);
            py::buffer_info buf = farr.request();
            keepAlive.push_back(py::array(farr));  // keep alive
            inputBuffers.push_back(reinterpret_cast<uint8_t*>(buf.ptr));
        } else {
            // Native mode: preserve dtype, but ensure contiguous C-style memory.
            py::array carr = py::array::ensure(input[i], py::array::c_style);
            if (!carr) {
                throw std::runtime_error("Failed to ensure contiguous input array for model: " + model_name);
            }
            py::buffer_info buf = carr.request();
            keepAlive.push_back(carr);
            inputBuffers.push_back(reinterpret_cast<uint8_t*>(buf.ptr));
        }
    }

    g_LibAppBuilder.ModelInference(model_name, inputBuffers, outputBuffers, outputSize, perf_profile, graphIndex);

    //QNN_INF("inference::inference output vector length: %d\n", outputBuffers.size());

    // dtype list like: ['float16', 'float16', ...]
    std::vector<std::string> outDtypes = g_LibAppBuilder.getOutputDataType(model_name);
    // output shapes for robust element count & float/native dtype inference
    std::vector<std::vector<size_t>> outShapes = g_LibAppBuilder.getOutputShapes(model_name);

    std::vector<py::array> output;

    //start_time();
    for (auto i = 0; i < outputBuffers.size(); i++) {
        std::string dtypeStr = (i < outDtypes.size()) ? outDtypes[i] : std::string("uint8");
        const std::vector<size_t> shape = (i < outShapes.size()) ? outShapes[i] : std::vector<size_t>{};

        // infer real dtype from outputSize & outputShape to support FLOAT_ONLY outputs
        py::dtype dt = inferOutputNumpyDtype((i < outputSize.size()) ? outputSize[i] : 0, shape, dtypeStr);

        // element count: prefer shape product (stable for both native & float)
        size_t elemCount = 0;
        if (!shape.empty()) {
            elemCount = productDims(shape);
        } else {
            // fallback to old method if shape unavailable
            const size_t itemBytes = static_cast<size_t>(dt.itemsize());
            if (itemBytes > 0 && i < outputSize.size() && (outputSize[i] % itemBytes) == 0) {
                elemCount = outputSize[i] / itemBytes;
            } else if (i < outputSize.size()) {
                dt = py::dtype::of<uint8_t>();
                elemCount = outputSize[i];
            }
        }

        // https://github.com/pybind/pybind11/issues/1042#issuecomment-325941022
        // Avoid memory copy for saving time. 'py::capsule' for freeing the memory.
        py::capsule free_data(outputBuffers[i], [](void* f) {free(f);});
        py::array result(dt,
                        { static_cast<py::ssize_t>(elemCount) },
                        { static_cast<py::ssize_t>(dt.itemsize()) },
                        outputBuffers[i],
                        free_data);
        output.push_back(result);
    }
    //print_time("convert Data To ArrayV");

    return output;
}

std::vector<py::array> inference_P(std::string model_name, std::string proc_name, std::string share_memory_name,
                                   const std::vector<py::array>& input, std::string perf_profile, size_t graphIndex = 0, 
                                   const std::string& input_data_type="float", const std::string& output_data_type="float") {
    std::vector<uint8_t*> inputBuffers;
    std::vector<size_t> inputSize;
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;

    // Keep temporary converted/contiguous arrays alive during ModelInference
    std::vector<py::array> keepAlive;
    const bool floatMode = (input_data_type == "float");

    for (auto i = 0; i < input.size(); i++) {
        if (floatMode) {
            py::array_t<float, py::array::c_style | py::array::forcecast> farr(input[i]);
            py::buffer_info buf = farr.request();
            keepAlive.push_back(py::array(farr));
            inputBuffers.push_back(reinterpret_cast<uint8_t*>(buf.ptr));
            // float32 bytes
            size_t bytes = static_cast<size_t>(buf.size) * static_cast<size_t>(buf.itemsize);
            inputSize.push_back(bytes);
        } else {
            py::array carr = py::array::ensure(input[i], py::array::c_style);
            if (!carr) {
                throw std::runtime_error("Failed to ensure contiguous input array for model: " + model_name);
            }
            py::buffer_info buf = carr.request();
            keepAlive.push_back(carr);
            inputBuffers.push_back(reinterpret_cast<uint8_t*>(buf.ptr));
            // native bytes
            size_t bytes = static_cast<size_t>(buf.size) * static_cast<size_t>(buf.itemsize);
            inputSize.push_back(bytes);
        }
    }

    g_LibAppBuilder.ModelInference(model_name, proc_name, share_memory_name, inputBuffers, inputSize, outputBuffers, outputSize, perf_profile, graphIndex);

    //QNN_INF("inference_P::inference output vector length: %d\n", outputBuffers.size());

    // dtype list like: ['float16', 'float16', ...]
    std::vector<std::string> outDtypes = g_LibAppBuilder.getOutputDataType(model_name, proc_name);
    std::vector<std::vector<size_t>> outShapes = g_LibAppBuilder.getOutputShapes(model_name, proc_name);

    std::vector<py::array> output;

    //start_time();
    for (auto i = 0; i < outputBuffers.size(); i++) {
        std::string dtypeStr = (i < outDtypes.size()) ? outDtypes[i] : std::string("uint8");
        const std::vector<size_t> shape = (i < outShapes.size()) ? outShapes[i] : std::vector<size_t>{};

        py::dtype dt = inferOutputNumpyDtype((i < outputSize.size()) ? outputSize[i] : 0, shape, dtypeStr);

        size_t elemCount = 0;
        if (!shape.empty()) {
            elemCount = productDims(shape);
        } else {
            const size_t itemBytes = static_cast<size_t>(dt.itemsize());
            if (itemBytes > 0 && i < outputSize.size() && (outputSize[i] % itemBytes) == 0) {
                elemCount = outputSize[i] / itemBytes;
            } else if (i < outputSize.size()) {
                dt = py::dtype::of<uint8_t>();
                elemCount = outputSize[i];
            }
        }

        // https://github.com/pybind/pybind11/issues/1042#issuecomment-325941022
        // Avoid memory copy for saving time. 'py::capsule' for freeing the memory.
        py::capsule free_data(outputBuffers[i], [](void* f) {});  // Not free this memory since it's share memory.
        py::array result(dt,
                        { static_cast<py::ssize_t>(elemCount) },
                        { static_cast<py::ssize_t>(dt.itemsize()) },
                        outputBuffers[i],
                        free_data);
        output.push_back(result);
    }
    //print_time("convert Data To ArrayV");

    return output;
}

bool ApplyBinaryUpdate(const std::vector<LoraAdapter>& lora_adapters);

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
    std::vector<LoraAdapter> m_lora_adapters;  

    QNNContext(const std::string& model_name, const std::string& model_path, const std::string& backend_lib_path, const std::string& system_lib_path, 
               bool async = false, const std::string& input_data_type="float", const std::string& output_data_type="float");

    QNNContext(const std::string& model_name, const std::string& model_path, const std::string& backend_lib_path, const std::string& system_lib_path, const std::vector<LoraAdapter>& lora_adapters, 
               bool async = false, const std::string& input_data_type="float", const std::string& output_data_type="float");   

    QNNContext(const std::string& model_name, const std::string& proc_name, const std::string& model_path, const std::string& backend_lib_path, const std::string& system_lib_path, 
               bool async = false, const std::string& input_data_type="float", const std::string& output_data_type="float");
    
    std::vector<py::array> Inference(const std::vector<py::array>& input, const std::string& perf_profile = "default", size_t graphIndex = 0, const std::string& input_data_type="float", const std::string& output_data_type="float");
    std::vector<py::array> Inference(const ShareMemory& share_memory, const std::vector<py::array>& input, const std::string& perf_profile = "default", size_t graphIndex = 0, const std::string& input_data_type="float", const std::string& output_data_type="float");

    bool ApplyBinaryUpdate(const std::vector<LoraAdapter>& lora_adapters);

    // issue#24
    std::vector<std::vector<size_t>> getInputShapes();
    std::vector<std::string>  getInputDataType();
    std::vector<std::string>  getOutputDataType();
    std::vector<std::vector<size_t>> getOutputShapes();
    std::string getGraphName();
    std::vector<std::string>  getInputName();
    std::vector<std::string>  getOutputName();

    std::vector<std::vector<size_t>> getInputShapes(const std::string& proc_name);
    std::vector<std::string>  getInputDataType(const std::string& proc_name);
    std::vector<std::string>  getOutputDataType(const std::string& proc_name);
    std::vector<std::vector<size_t>> getOutputShapes(const std::string& proc_name);
    std::string getGraphName(const std::string& proc_name);
    std::vector<std::string>  getInputName(const std::string& proc_name);
    std::vector<std::string>  getOutputName(const std::string& proc_name);

    typedef struct ModelInfo {
        std::vector<std::vector<size_t>> inputShapes;
        std::vector<std::string>  inputDataType;
        std::vector<std::vector<size_t>> onputShapes;
        std::vector<std::string> onputDataType;
        std::string graphName;
    } ModelInfo_t;
    ~QNNContext();
};

#endif

