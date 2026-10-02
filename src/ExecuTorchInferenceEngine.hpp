//==============================================================================
//
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

#include <executorch/extension/module/module.h>
#include <executorch/runtime/core/exec_aten/exec_aten.h>
#include <executorch/runtime/executor/method_meta.h>

namespace qnn {
namespace tools {
namespace qnn_app {

/// Direct ExecuTorch engine for static, single-method tensor programs.
/// Backend selection is encoded in the .pte program; the engine reports the
/// backend used by the loaded forward method.
class ExecuTorchInferenceEngine {
 public:
  ExecuTorchInferenceEngine(std::string model_path,
                            std::string backend_lib_path = "");
  ExecuTorchInferenceEngine(const ExecuTorchInferenceEngine&) = delete;
  ExecuTorchInferenceEngine& operator=(const ExecuTorchInferenceEngine&) = delete;
  ~ExecuTorchInferenceEngine() noexcept;

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
  static std::string scalarTypeName(executorch::aten::ScalarType type);
  static std::string modelError(const std::string& operation,
                                const std::string& model_path,
                                const std::string& detail);
  void cacheMetadataLocked();
  void updateProviderModeLocked();

  std::string m_model_path;
  std::string m_backend_lib_path;
  std::unique_ptr<executorch::extension::Module> m_module;
  std::optional<executorch::runtime::MethodMeta> m_method_meta;
  std::vector<std::vector<size_t>> m_input_shapes;
  std::vector<std::vector<size_t>> m_output_shapes;
  std::vector<std::string> m_input_types;
  std::vector<std::string> m_output_types;
  std::vector<std::string> m_input_names;
  std::vector<std::string> m_output_names;
  std::vector<size_t> m_input_nbytes;
  std::vector<size_t> m_output_nbytes;
  std::string m_provider_mode;
  void* m_backend_handle = nullptr;
  mutable std::mutex m_inference_mutex;
};

}  // namespace qnn_app
}  // namespace tools
}  // namespace qnn
