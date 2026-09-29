//==============================================================================
//
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "TFLiteInferenceEngine.hpp"

#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <utility>

namespace qnn {
namespace tools {
namespace qnn_app {
namespace {

std::vector<uint8_t> readModel(const std::string& path) {
  std::ifstream file(path, std::ios::binary | std::ios::ate);
  if (!file) {
    throw std::runtime_error("TFLite model load failed for '" + path + "': file is unreadable");
  }
  const std::streamoff size = file.tellg();
  if (size <= 0 || static_cast<uint64_t>(size) > std::numeric_limits<size_t>::max()) {
    throw std::runtime_error("TFLite model load failed for '" + path + "': file is empty or too large");
  }
  std::vector<uint8_t> bytes(static_cast<size_t>(size));
  file.seekg(0, std::ios::beg);
  if (!file.read(reinterpret_cast<char*>(bytes.data()), size)) {
    throw std::runtime_error("TFLite model load failed for '" + path + "': failed to read model bytes");
  }
  return bytes;
}

}  // namespace

TFLiteInferenceEngine::TFLiteInferenceEngine(std::string model_path,
                                             std::string backend_lib_path,
                                             bool use_qnn_delegate,
                                             int num_threads)
    : m_model_path(std::move(model_path)),
      m_backend_lib_path(std::move(backend_lib_path)),
      m_use_qnn_delegate(use_qnn_delegate),
      m_num_threads(num_threads > 0 ? num_threads : 1) {}

TFLiteInferenceEngine::~TFLiteInferenceEngine() noexcept { release(); }

std::string TFLiteInferenceEngine::modelError(const std::string& operation,
                                              const std::string& model_path,
                                              const std::string& detail) {
  return "TFLite model '" + model_path + "' " + operation + " failed: " + detail;
}

void TFLiteInferenceEngine::initialize() {
  if (m_interpreter != nullptr) {
    return;
  }

  m_model_bytes = readModel(m_model_path);
  m_model = TfLiteModelCreate(m_model_bytes.data(), m_model_bytes.size());
  if (m_model == nullptr) {
    throw std::runtime_error(modelError("model validation", m_model_path,
                                        "invalid or unsupported FlatBuffer"));
  }

  m_options = TfLiteInterpreterOptionsCreate();
  if (m_options == nullptr) {
    release();
    throw std::runtime_error(modelError("interpreter setup", m_model_path,
                                        "could not allocate interpreter options"));
  }
  TfLiteInterpreterOptionsSetNumThreads(m_options, m_num_threads);

#if defined(APPBUILDER_ENABLE_TFLITE) && !defined(APPBUILDER_ENABLE_TFLITE_CPU)
  if (m_use_qnn_delegate) {
    TfLiteQnnDelegateOptions delegate_options = TfLiteQnnDelegateOptionsDefault();
    delegate_options.backend_type = kHtpBackend;
    delegate_options.library_path = m_backend_lib_path.empty() ? nullptr : m_backend_lib_path.c_str();
    delegate_options.log_level = kLogLevelError;
    delegate_options.htp_options.performance_mode = kHtpBurst;
    delegate_options.htp_options.perf_ctrl_strategy = kHtpPerfCtrlAuto;
    delegate_options.htp_options.useConvHmx = true;
    delegate_options.htp_options.useFoldRelu = true;
    delegate_options.htp_options.optimization_strategy = kHtpOptimizeForInferenceO3;
    delegate_options.graph_priority = kQnnPriorityHigh;
    if (const char* skel_dir = std::getenv("QNN_SKEL_DIR"); skel_dir != nullptr && skel_dir[0] != '\0') {
      delegate_options.skel_library_dir = skel_dir;
    }
    m_delegate = TfLiteQnnDelegateCreate(&delegate_options);
    if (m_delegate == nullptr) {
      release();
      throw std::runtime_error(modelError("delegate creation", m_model_path,
                                          "QNN TFLite delegate returned null"));
    }
    TfLiteInterpreterOptionsAddDelegate(m_options, m_delegate);
  }
#else
  if (m_use_qnn_delegate) {
    release();
    throw std::runtime_error(modelError("delegate setup", m_model_path,
                                        "QNN delegate support is not compiled"));
  }
#endif

  m_interpreter = TfLiteInterpreterCreate(m_model, m_options);
  if (m_interpreter == nullptr) {
    release();
    throw std::runtime_error(modelError("interpreter creation", m_model_path,
                                        "TFLite interpreter creation failed"));
  }
  m_delegate_attached = m_use_qnn_delegate;

  const TfLiteStatus allocation_status = TfLiteInterpreterAllocateTensors(m_interpreter);
  if (allocation_status != kTfLiteOk) {
    release();
    throw std::runtime_error(modelError("tensor allocation", m_model_path,
                                        "delegate-backed tensor allocation failed"));
  }
}

void TFLiteInferenceEngine::release() noexcept {
  std::lock_guard<std::mutex> lock(m_inference_mutex);
  if (m_interpreter != nullptr) {
    TfLiteInterpreterDelete(m_interpreter);
    m_interpreter = nullptr;
  }
#if defined(APPBUILDER_ENABLE_TFLITE) && !defined(APPBUILDER_ENABLE_TFLITE_CPU)
  if (m_delegate != nullptr) {
    TfLiteQnnDelegateDelete(m_delegate);
    m_delegate = nullptr;
  }
#else
  m_delegate = nullptr;
#endif
  if (m_options != nullptr) {
    TfLiteInterpreterOptionsDelete(m_options);
    m_options = nullptr;
  }
  if (m_model != nullptr) {
    TfLiteModelDelete(m_model);
    m_model = nullptr;
  }
  m_model_bytes.clear();
  m_delegate_attached = false;
}

void TFLiteInferenceEngine::validateInitialized(const char* operation) const {
  if (m_interpreter == nullptr) {
    throw std::runtime_error(modelError(operation, m_model_path,
                                        "context has not been initialized or was released"));
  }
}

void TFLiteInferenceEngine::validateGraphIndex(size_t graph_index) const {
  if (graph_index != 0) {
    throw std::out_of_range(modelError("graph selection", m_model_path,
                                       "graphIndex must be 0 for a TFLite interpreter"));
  }
}

std::vector<std::vector<uint8_t>> TFLiteInferenceEngine::inference(
    const std::vector<const uint8_t*>& input_buffers,
    const std::vector<size_t>& input_sizes,
    size_t graph_index) {
  std::lock_guard<std::mutex> lock(m_inference_mutex);
  validateInitialized("inference");
  validateGraphIndex(graph_index);

  const int input_count = TfLiteInterpreterGetInputTensorCount(m_interpreter);
  if (input_buffers.size() != static_cast<size_t>(input_count) ||
      input_sizes.size() != static_cast<size_t>(input_count)) {
    throw std::invalid_argument(modelError("inference", m_model_path,
                                           "input tensor count does not match the model"));
  }
  for (int i = 0; i < input_count; ++i) {
    const TfLiteTensor* tensor = TfLiteInterpreterGetInputTensor(m_interpreter, i);
    if (tensor == nullptr) {
      throw std::runtime_error(modelError("inference", m_model_path,
                                          "input tensor metadata is unavailable"));
    }
    const size_t required = TfLiteTensorByteSize(tensor);
    if (input_buffers[i] == nullptr || input_sizes[i] != required) {
      std::ostringstream detail;
      detail << "input " << i << " byte size is " << input_sizes[i]
             << ", expected " << required;
      throw std::invalid_argument(modelError("inference", m_model_path, detail.str()));
    }
    void* destination = TfLiteTensorData(tensor);
    if (destination == nullptr) {
      throw std::runtime_error(modelError("inference", m_model_path,
                                          "input tensor storage is unavailable"));
    }
    std::memcpy(destination, input_buffers[i], required);
  }

  if (TfLiteInterpreterInvoke(m_interpreter) != kTfLiteOk) {
    throw std::runtime_error(modelError("inference", m_model_path,
                                        m_use_qnn_delegate
                                            ? "QNN delegate invocation failed"
                                            : "CPU interpreter invocation failed"));
  }

  const int output_count = TfLiteInterpreterGetOutputTensorCount(m_interpreter);
  std::vector<std::vector<uint8_t>> outputs;
  outputs.reserve(static_cast<size_t>(output_count));
  for (int i = 0; i < output_count; ++i) {
    const TfLiteTensor* tensor = TfLiteInterpreterGetOutputTensor(m_interpreter, i);
    if (tensor == nullptr || TfLiteTensorData(tensor) == nullptr) {
      throw std::runtime_error(modelError("inference", m_model_path,
                                          "output tensor storage is unavailable"));
    }
    const size_t bytes = TfLiteTensorByteSize(tensor);
    const auto* data = static_cast<const uint8_t*>(TfLiteTensorData(tensor));
    outputs.emplace_back(data, data + bytes);
  }
  return outputs;
}

std::vector<size_t> TFLiteInferenceEngine::tensorShape(const TfLiteTensor* tensor) {
  if (tensor == nullptr) {
    return {};
  }
  const int32_t rank = TfLiteTensorNumDims(tensor);
  if (rank < 0) {
    throw std::runtime_error("TFLite tensor has invalid rank metadata");
  }
  std::vector<size_t> result;
  result.reserve(static_cast<size_t>(rank));
  for (int32_t i = 0; i < rank; ++i) {
    const int32_t dimension = TfLiteTensorDim(tensor, i);
    if (dimension < 0) {
      throw std::runtime_error("TFLite tensor has a dynamic dimension that is not supported");
    }
    result.push_back(static_cast<size_t>(dimension));
  }
  return result;
}

std::string TFLiteInferenceEngine::tensorTypeName(TfLiteType type) {
  switch (type) {
    case kTfLiteFloat32: return "float32";
    case kTfLiteFloat16: return "float16";
    case kTfLiteInt8: return "int8";
    case kTfLiteUInt8: return "uint8";
    case kTfLiteInt16: return "int16";
    case kTfLiteUInt16: return "uint16";
    case kTfLiteInt32: return "int32";
    case kTfLiteUInt32: return "uint32";
    case kTfLiteInt64: return "int64";
    case kTfLiteUInt64: return "uint64";
    case kTfLiteBool: return "bool";
    default: return "unknown";
  }
}

std::vector<std::vector<size_t>> TFLiteInferenceEngine::getInputShapes(size_t graph_index) const {
  validateInitialized("input shape query");
  validateGraphIndex(graph_index);
  std::vector<std::vector<size_t>> result;
  const int count = TfLiteInterpreterGetInputTensorCount(m_interpreter);
  for (int i = 0; i < count; ++i) result.push_back(tensorShape(TfLiteInterpreterGetInputTensor(m_interpreter, i)));
  return result;
}

std::vector<std::vector<size_t>> TFLiteInferenceEngine::getOutputShapes(size_t graph_index) const {
  validateInitialized("output shape query");
  validateGraphIndex(graph_index);
  std::vector<std::vector<size_t>> result;
  const int count = TfLiteInterpreterGetOutputTensorCount(m_interpreter);
  for (int i = 0; i < count; ++i) result.push_back(tensorShape(TfLiteInterpreterGetOutputTensor(m_interpreter, i)));
  return result;
}

std::vector<std::string> TFLiteInferenceEngine::getInputDataType(size_t graph_index) const {
  validateInitialized("input dtype query");
  validateGraphIndex(graph_index);
  std::vector<std::string> result;
  const int count = TfLiteInterpreterGetInputTensorCount(m_interpreter);
  for (int i = 0; i < count; ++i) result.push_back(tensorTypeName(TfLiteTensorType(TfLiteInterpreterGetInputTensor(m_interpreter, i))));
  return result;
}

std::vector<std::string> TFLiteInferenceEngine::getOutputDataType(size_t graph_index) const {
  validateInitialized("output dtype query");
  validateGraphIndex(graph_index);
  std::vector<std::string> result;
  const int count = TfLiteInterpreterGetOutputTensorCount(m_interpreter);
  for (int i = 0; i < count; ++i) result.push_back(tensorTypeName(TfLiteTensorType(TfLiteInterpreterGetOutputTensor(m_interpreter, i))));
  return result;
}

std::vector<std::string> TFLiteInferenceEngine::getInputName(size_t graph_index) const {
  validateInitialized("input name query");
  validateGraphIndex(graph_index);
  std::vector<std::string> result;
  const int count = TfLiteInterpreterGetInputTensorCount(m_interpreter);
  for (int i = 0; i < count; ++i) {
    const char* name = TfLiteTensorName(TfLiteInterpreterGetInputTensor(m_interpreter, i));
    result.emplace_back(name != nullptr ? name : "");
  }
  return result;
}

std::vector<std::string> TFLiteInferenceEngine::getOutputName(size_t graph_index) const {
  validateInitialized("output name query");
  validateGraphIndex(graph_index);
  std::vector<std::string> result;
  const int count = TfLiteInterpreterGetOutputTensorCount(m_interpreter);
  for (int i = 0; i < count; ++i) {
    const char* name = TfLiteTensorName(TfLiteInterpreterGetOutputTensor(m_interpreter, i));
    result.emplace_back(name != nullptr ? name : "");
  }
  return result;
}

std::string TFLiteInferenceEngine::getGraphName(size_t graph_index) const {
  validateInitialized("graph name query");
  validateGraphIndex(graph_index);
  return m_model_path;
}

uint64_t TFLiteInferenceEngine::getProfilingEvent(uint32_t) const {
  validateInitialized("profiling query");
  return 0;
}

std::string TFLiteInferenceEngine::getProviderMode() const {
  validateInitialized("provider query");
  return m_delegate_attached ? "qnn" : "cpu";
}

}  // namespace qnn_app
}  // namespace tools
}  // namespace qnn
