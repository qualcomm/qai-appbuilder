//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "AppBuilder.h"
#include "common.h"
#include "Lora.hpp"
#include <ostream>

ShareMemory::ShareMemory(const std::string& share_memory_name, const size_t share_memory_size) {
    m_share_memory_name = share_memory_name;
    g_LibAppBuilder.CreateShareMemory(share_memory_name, share_memory_size);
}

ShareMemory::~ShareMemory() {
    g_LibAppBuilder.DeleteShareMemory(m_share_memory_name);
}

QNNContext::QNNContext(const std::string& model_name,
                       const std::string& model_path, const std::string& backend_lib_path, const std::string& system_lib_path, bool async) {
    m_model_name = model_name;

    g_LibAppBuilder.ModelInitialize(model_name, model_path, backend_lib_path, system_lib_path, async);
}

QNNContext::QNNContext(const std::string& model_name, const std::string& proc_name,
                       const std::string& model_path, const std::string& backend_lib_path, const std::string& system_lib_path, bool async) {
    m_model_name = model_name;
    m_proc_name = proc_name;

    g_LibAppBuilder.ModelInitialize(model_name, proc_name, model_path, backend_lib_path, system_lib_path, async);
}

QNNContext::QNNContext(const std::string& model_name,
                       const std::string& model_path, const std::string& backend_lib_path, 
                       const std::string& system_lib_path, const std::vector<LoraAdapter>& lora_adapters, bool async) {
    
    m_model_name = model_name;
    m_lora_adapters = lora_adapters;

    g_LibAppBuilder.ModelInitialize(model_name, model_path, backend_lib_path, system_lib_path, m_lora_adapters, async);
}

// issue#24
std::vector<std::vector<size_t>> QNNContext::getInputShapes(){
    return g_LibAppBuilder.getInputShapes(m_model_name);
};

std::vector<std::string> QNNContext::getInputDataType(){
    return g_LibAppBuilder.getInputDataType(m_model_name);
};

std::vector<std::string> QNNContext::getOutputDataType(){
    return g_LibAppBuilder.getOutputDataType(m_model_name);
};

std::vector<std::vector<size_t>> QNNContext::getOutputShapes(){
    return g_LibAppBuilder.getOutputShapes(m_model_name);
};

QNNContext::~QNNContext() {
    if (m_proc_name.empty())
        g_LibAppBuilder.ModelDestroy(m_model_name);
    else
        g_LibAppBuilder.ModelDestroy(m_model_name, m_proc_name);
}


std::vector<py::array_t<float>> 
QNNContext::Inference(const std::vector<py::array_t<float>>& input, const std::string& perf_profile, size_t graphIndex) {
    return inference(m_model_name, input, perf_profile, graphIndex);
}

std::vector<py::array_t<float>> 
QNNContext::Inference(const ShareMemory& share_memory, const std::vector<py::array_t<float>>& input, const std::string& perf_profile, size_t graphIndex) {
    return inference_P(m_model_name, m_proc_name, share_memory.m_share_memory_name, input, perf_profile, graphIndex);
}

bool QNNContext::ApplyBinaryUpdate(const std::vector<LoraAdapter>& lora_adapters) {
    return g_LibAppBuilder.ModelApplyBinaryUpdate(m_model_name, const_cast<std::vector<LoraAdapter>&>(lora_adapters));
}

PYBIND11_MODULE(appbuilder, m) {
    m.doc() = R"pbdoc(
        Pybind11 AppBuilder Extension.
        -----------------------
        .. currentmodule:: qai_appbuilder
        .. autosummary::
            :toctree: _generate

            model_initialize
            model_inference
            model_destroy
            memory_create
            memory_delete
            set_log_level
            set_profiling_level
            set_perf_profile
            rel_perf_profile
            )pbdoc";

    m.attr("__name__") = "qai_appbuilder";
    m.attr("__version__") = APPBUILDER_VERSION;
    m.attr("__author__") = "quic-zhanweiw";
    m.attr("__license__") = "BSD-3-Clause";

    m.def("model_initialize", &initialize, "Initialize models.");

#ifdef _WIN32
    m.def("model_initialize", &initialize_P, "Initialize models.");
#endif

    m.def("model_inference", &inference, "Inference models.");

#ifdef _WIN32
    m.def("model_inference", &inference_P, "Inference models.");
#endif

#ifdef _WIN32
    m.def("model_destroy", &destroy, "Destroy models.");
#else
    m.def("model_destroy", static_cast<int(*)(std::string)>(&destroy), "Destroy models.");
#endif

#ifdef _WIN32
    m.def("model_destroy", &destroy_P, "Destroy models.");
#endif

    m.def("memory_create", &create_memory, "Create share memory.");
    m.def("memory_delete", &delete_memory, "Delete share memory.");
    m.def("set_log_level", &set_log_level, "Set QNN log level.");
    m.def("set_profiling_level", &set_profiling_level, "Set QNN profiling level.");
    m.def("set_perf_profile", &set_perf_profile, "Set HTP perf profile.");
    m.def("rel_perf_profile", &rel_perf_profile, "Release HTP perf profile.");


    py::class_<ShareMemory>(m, "ShareMemory")
        .def(py::init<const std::string&, const size_t>());

    py::class_<QNNContext>(m, "QNNContext")
        .def(py::init<const std::string&, const std::string&, const std::string&, const std::string&, bool>())
        .def(py::init<const std::string&, const std::string&, const std::string&, const std::string&, const std::vector<LoraAdapter>&, bool>())
        .def(py::init<const std::string&, const std::string&, const std::string&, const std::string&, const std::string&, bool>())
        .def("Inference", py::overload_cast<const std::vector<py::array_t<float>>&, const std::string&, size_t>(&QNNContext::Inference))
        .def("Inference", py::overload_cast<const ShareMemory&, const std::vector<py::array_t<float>>&, const std::string&, size_t>(&QNNContext::Inference))
        .def("ApplyBinaryUpdate", &QNNContext::ApplyBinaryUpdate, "Apply Lora binary update")
        .def("getInputShapes", &QNNContext::getInputShapes, "Get Input Shape")
        .def("getInputDataType", &QNNContext::getInputDataType, "Get Input data type")
        .def("getOutputDataType", &QNNContext::getOutputDataType, "Get output data type")
        .def("getOutputShapes", &QNNContext::getOutputShapes, "Get Output Shape");


    py::class_<LoraAdapter>(m, "LoraAdapter")
        .def(py::init<const std::string &, const std::vector<std::string> &>());
}

