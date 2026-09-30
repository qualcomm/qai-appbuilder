//==============================================================================
//
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
#pragma once

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

#include "tensorflow/lite/c/c_api.h"
#if defined(APPBUILDER_ENABLE_TFLITE) && !defined(APPBUILDER_ENABLE_TFLITE_CPU)
#include "QNN/TFLiteDelegate/QnnTFLiteDelegate.h"
#endif

namespace qnn {
namespace tools {
namespace qnn_app {

/// Direct TFLite interpreter engine. The CPU build uses the TFLite builtin
/// kernels, while the Linux ARM64 build can attach the QNN TFLite delegate.
class TFLiteInferenceEngine {
 public:
  TFLiteInferenceEngine(std::string model_path,
                         std::string backend_lib_path = "",
                         bool use_qnn_delegate = true,
                         int num_threads = 1);
  TFLiteInferenceEngine(const TFLiteInferenceEngine&) = delete;
  TFLiteInferenceEngine& operator=(const TFLiteInferenceEngine&) = delete;
  ~TFLiteInferenceEngine() noexcept;

  void initialize();
  void release() noexcept;

  std::vector<std::vector<uint8_t>> inference(
      const std::vector<const uint8_t*>& input_buffers,
      const std::vector<size_t>& input_sizes,
      size_t graph_index = 0);

  std::vector<std::vector<size_t>> getInputShapes(size_t graph_index = 0) const;
  std::vector<std::vector<size_t>> getOutputShapes(size_t graph_index = 0) const;
  std::vector<std::string> getInputDataType(size_t graph_index = 0) const;
  std::vector<std::string> getOutputDataType(size_t graph_index = 0) const;
  std::vector<std::string> getInputName(size_t graph_index = 0) const;
  std::vector<std::string> getOutputName(size_t graph_index = 0) const;
  std::string getGraphName(size_t graph_index = 0) const;
  uint64_t getProfilingEvent(uint32_t event_type) const;
  std::string getProviderMode() const;

 private:
  void releaseLocked() noexcept;
  void validateGraphIndex(size_t graph_index) const;
  void validateInitialized(const char* operation) const;
  static std::string tensorTypeName(TfLiteType type);
  static std::vector<size_t> tensorShape(const TfLiteTensor* tensor);
  static std::string modelError(const std::string& operation,
                                const std::string& model_path,
                                const std::string& detail);

  std::string m_model_path;
  std::string m_backend_lib_path;
  bool m_use_qnn_delegate{true};
  int m_num_threads{1};
  std::vector<uint8_t> m_model_bytes;
  TfLiteModel* m_model{nullptr};
  TfLiteInterpreterOptions* m_options{nullptr};
  TfLiteDelegate* m_delegate{nullptr};
  TfLiteInterpreter* m_interpreter{nullptr};
  bool m_delegate_attached{false};
  mutable std::mutex m_inference_mutex;
};

}  // namespace qnn_app
}  // namespace tools
}  // namespace qnn
