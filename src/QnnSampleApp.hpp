//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
#pragma once

#include <memory>
#include <queue>

#include "IOTensor.hpp"
#include "SampleApp.hpp"
#include "Lora.hpp"

// zw: For supporting BackendExtensions.
#include "HTP/QnnHtpPerfInfrastructure.h"
#include "HTP/QnnHtpDevice.h"


bool disableDcvs(QnnHtpDevice_PerfInfrastructure_t perfInfra);
bool enableDcvs(QnnHtpDevice_PerfInfrastructure_t perfInfra);
bool boostPerformance(QnnHtpDevice_PerfInfrastructure_t perfInfra, std::string perfProfile);
bool resetPerformance(QnnHtpDevice_PerfInfrastructure_t perfInfra);

namespace qnn {
namespace tools {
namespace sample_app {

struct RunTimeAppKeys {
    std::string backendLibKey{ "NetRunBackendLibKeyDefault" };
    std::string backendHandlerKey{ "NetRunBackendHandlerDefault" };
    std::string modelLibKey{ "NetRunModelKeyDefault" };
    std::string loggerKey{ "NetRunLoggerKeyDefault" };
    std::string deviceKey{ "NetRunDeviceKeyDefault" };
    std::string contextKey{ "NetRunContextKeyDefault" };
};

enum class StatusCode {
  SUCCESS,
  FAILURE,
  FAILURE_INPUT_LIST_EXHAUSTED,
  FAILURE_SYSTEM_ERROR,
  FAILURE_SYSTEM_COMMUNICATION_ERROR,
  QNN_FEATURE_UNSUPPORTED
};

class QnnSampleApp {
 public:
  QnnSampleApp(QnnFunctionPointers qnnFunctionPointers,
               std::string inputListPaths,
               std::string opPackagePaths,
               void *backendHandle,
               std::string outputPath                  = s_defaultOutputPath,
               bool debug                              = false,
               iotensor::OutputDataType outputDataType = iotensor::OutputDataType::FLOAT_ONLY,
               iotensor::InputDataType inputDataType   = iotensor::InputDataType::FLOAT,
               ProfilingLevel profilingLevel           = ProfilingLevel::OFF,
               bool dumpOutputs                        = false,
               std::string cachedBinaryPath            = "",
               std::string saveBinaryName              = "",
               const std::vector<LoraAdapter>& lora_adapters = std::vector<LoraAdapter>());

  // @brief Print a message to STDERR then return a nonzero
  //  exit status.
  int32_t reportError(const std::string &err);

  StatusCode initialize();

  StatusCode initializeBackend();

  StatusCode createContext();

  StatusCode composeGraphs();

  StatusCode finalizeGraphs();

  StatusCode executeGraphs();

  StatusCode registerOpPackages();

  StatusCode createFromBinary();

  StatusCode saveBinary();

  StatusCode freeContext();

  StatusCode terminateBackend();

  StatusCode freeGraphs();  // zw.
  StatusCode initializeProfiling();

  std::string getBackendBuildId();

  StatusCode isDevicePropertySupported();

  StatusCode isFinalizeDeserializedGraphSupported();

  StatusCode createDevice();

  StatusCode contextApplyBinarySection(QnnContext_SectionType_t section);
  bool binaryUpdates();
  void update_m_lora_adapters(std::vector<LoraAdapter>& lora_adapters);

  StatusCode applyBinarySection(
      std::string graphName,
      std::string binaryPath,
      QnnContext_SectionType_t sectionType,
      bool useMmap,
      ProfilingLevel profilingLevel,
      ProfilingOption profilingOption);

  StatusCode initializeProfileHandle(
      const QNN_INTERFACE_VER_TYPE* qnnInterfaceHandle,
      ProfilingLevel profilingLevel,
      Qnn_ProfileHandle_t* profileHandle,
      const uint64_t numMaxEvents);

  StatusCode initializeProfileConfigOption(
      const QNN_INTERFACE_VER_TYPE* qnnInterfaceHandle,
      ProfilingOption profilingOption,
      Qnn_ProfileHandle_t profileHandle);

  StatusCode terminateProfileHandle(
      const QNN_INTERFACE_VER_TYPE* qnnInterfaceHandle, Qnn_ProfileHandle_t profileHandle);

  StatusCode addGraphsToContext(
      qnn_wrapper_api::GraphInfo_t** graphInfos, uint32_t numGraphs);

  StatusCode addGraphToContext(
      qnn_wrapper_api::GraphInfo_t* graphInfo);

  StatusCode freeDevice();

  StatusCode verifyFailReturnStatus(Qnn_ErrorHandle_t errCode);

// improve performance.
  StatusCode setupInputAndOutputTensors();
  StatusCode tearDownInputAndOutputTensors();

// zw.
  StatusCode executeGraphsBuffers(std::vector<uint8_t*>& inputBuffers,
                                  std::vector<uint8_t*>& outputBuffers, std::vector<size_t>& outputSize,
                                  std::string perfProfile, size_t graphIndex = 0);
  // issue#24
  std::vector<std::vector<size_t>> getInputShapes();
  std::vector<std::string> getInputDataType();
  std::vector<std::vector<size_t>> getOutputShapes();  
  std::vector<std::string> getOutputDataType();
  qnn_wrapper_api::GraphInfo_t **m_graphsInfo;
  uint32_t m_graphsCount;

  StatusCode initializeLog();
  StatusCode setLogLevel(QnnLog_Level_t logLevel);

  StatusCode initializePerformance();
  StatusCode destroyPerformance();

  virtual ~QnnSampleApp();

 private:
  StatusCode extractBackendProfilingInfo(Qnn_ProfileHandle_t profileHandle);

  StatusCode extractProfilingSubEvents(QnnProfile_EventId_t profileEventId);

  StatusCode extractProfilingEvent(QnnProfile_EventId_t profileEventId);
  
  static const std::string s_defaultOutputPath;

  QnnFunctionPointers m_qnnFunctionPointers;
  std::vector<std::string> m_inputListPaths;
  std::vector<std::vector<std::vector<std::string>>> m_inputFileLists;
  std::vector<std::unordered_map<std::string, uint32_t>> m_inputNameToIndex;
  std::vector<std::string> m_opPackagePaths;
  std::string m_outputPath;
  std::string m_saveBinaryName;
  std::string m_cachedBinaryPath;

  std::vector<LoraAdapter> m_lora_adapters;

  QnnBackend_Config_t **m_backendConfig = nullptr;
  Qnn_ContextHandle_t m_context         = nullptr;
  QnnContext_Config_t **m_contextConfig = nullptr;
  bool m_debug;
  iotensor::OutputDataType m_outputDataType;
  iotensor::InputDataType m_inputDataType;
  ProfilingLevel m_profilingLevel;
  bool m_dumpOutputs;
  //qnn_wrapper_api::GraphInfo_t **m_graphsInfo;
  //uint32_t m_graphsCount;
  iotensor::IOTensor m_ioTensor;
  bool m_isBackendInitialized;
  bool m_isContextCreated;
  Qnn_ProfileHandle_t m_profileBackendHandle              = nullptr;
  qnn_wrapper_api::GraphConfigInfo_t **m_graphConfigsInfo = nullptr;
  uint32_t m_graphConfigsInfoCount;
  Qnn_LogHandle_t m_logHandle         = nullptr;
  Qnn_BackendHandle_t m_backendHandle = nullptr;
  Qnn_DeviceHandle_t m_deviceHandle   = nullptr;
  RunTimeAppKeys m_runTimeAppKeys;
  uint64_t m_numMaxEvents = std::numeric_limits<uint64_t>::max();
  std::vector<qnn_wrapper_api::GraphInfo_t*> m_graphInfoPtrList;
  bool m_useMmap;
  ProfilingOption m_profilingOption;

  // zw.
  uint32_t m_powerConfigId = 1;
  QnnHtpDevice_PerfInfrastructure_t m_perfInfra = {nullptr};
  bool m_runInCpu = true;

  // issue#24
  std::vector<std::vector<size_t>> m_inputShapes;
  std::vector<std::string> m_inputDataType_s;
  std::vector<std::vector<size_t>> m_outputShapes;
  std::vector<std::string> m_outputDataType_s;
};
}  // namespace sample_app
}  // namespace tools
}  // namespace qnn

