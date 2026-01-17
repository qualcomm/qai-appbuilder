//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include <inttypes.h>

#include <cstring>
#include <fstream>
#include <iostream>

#include "DataUtil.hpp"
#include "Logger.hpp"
#include "PAL/Directory.hpp"
#include "PAL/FileOp.hpp"
#include "PAL/Path.hpp"
#include "PAL/StringOp.hpp"
#include "QnnDlcUtils.hpp"
#include "QnnSampleApp.hpp"
#include "QnnSampleAppUtils.hpp"
#include "QnnWrapperUtils.hpp"

// zw.
#include "QnnTypeMacros.hpp"
#include "IOTensor.hpp"
#include "LibAppBuilder.hpp"
#include <set>
#include <exception>

#ifdef _WIN32
#include <windows.h>
#endif

using namespace qnn;
using namespace qnn::tools;
using namespace qnn::tools::iotensor;

static const int sg_lowerLatency  = 40;    // Should be used on V66 and above only
static const int sg_lowLatency    = 100;   // This will limit sleep modes available while running
static const int sg_highLatency   = 2000;
static std::set<uint32_t> sg_powerConfigIds = {};

uint32_t getPowerConfigId() {
  if (sg_powerConfigIds.size() > 0) {
    return *sg_powerConfigIds.begin();
  }
  return 1;
}

bool disableDcvs(QnnHtpDevice_PerfInfrastructure_t perfInfra) {
  QnnHtpPerfInfrastructure_PowerConfig_t powerConfig;
  memset(&powerConfig, 0, sizeof(powerConfig));
  powerConfig.option                     = QNN_HTP_PERF_INFRASTRUCTURE_POWER_CONFIGOPTION_DCVS_V3;
  powerConfig.dcvsV3Config.dcvsEnable    = 0;  // FALSE
  powerConfig.dcvsV3Config.setDcvsEnable = 1;
  powerConfig.dcvsV3Config.powerMode     = QNN_HTP_PERF_INFRASTRUCTURE_POWERMODE_ADJUST_UP_DOWN;
  powerConfig.dcvsV3Config.contextId     = getPowerConfigId();

  const QnnHtpPerfInfrastructure_PowerConfig_t *powerConfigs[] = {&powerConfig, NULL};

  if (QNN_SUCCESS != perfInfra.setPowerConfig(getPowerConfigId(), powerConfigs)) {
    QNN_ERROR("Failure in setPowerConfig() from disableDcvs");
    return false;
  }
  return true;
}

bool enableDcvs(QnnHtpDevice_PerfInfrastructure_t perfInfra) {
  QnnHtpPerfInfrastructure_PowerConfig_t powerConfig;
  memset(&powerConfig, 0, sizeof(powerConfig));
  powerConfig.option                     = QNN_HTP_PERF_INFRASTRUCTURE_POWER_CONFIGOPTION_DCVS_V3;
  powerConfig.dcvsV3Config.dcvsEnable    = 1;
  powerConfig.dcvsV3Config.setDcvsEnable = 1;
  powerConfig.dcvsV3Config.powerMode     = QNN_HTP_PERF_INFRASTRUCTURE_POWERMODE_ADJUST_UP_DOWN;
  powerConfig.dcvsV3Config.contextId     = getPowerConfigId();

  const QnnHtpPerfInfrastructure_PowerConfig_t *powerConfigs[] = {&powerConfig, NULL};

  if (QNN_SUCCESS != perfInfra.setPowerConfig(getPowerConfigId(), powerConfigs)) {
    QNN_ERROR("Failure in setPowerConfig() from disableDcvs");
    return false;
  }
  return true;
}

bool boostPerformance(QnnHtpDevice_PerfInfrastructure_t perfInfra, std::string perfProfile) {
    // Initialize the power config and select the voltage corner values for the performance setting.
    QnnHtpPerfInfrastructure_PowerConfig_t powerConfig;
    memset(&powerConfig, 0, sizeof(powerConfig));

    QNN_INF("PERF::boostPerformance");

    powerConfig.option                     = QNN_HTP_PERF_INFRASTRUCTURE_POWER_CONFIGOPTION_DCVS_V3;
    powerConfig.dcvsV3Config.dcvsEnable    = 0;
    powerConfig.dcvsV3Config.setDcvsEnable = 1;
    powerConfig.dcvsV3Config.contextId     = getPowerConfigId();
  
    // refer QnnHtpPerfInfrastructure.h
    powerConfig.dcvsV3Config.powerMode = QNN_HTP_PERF_INFRASTRUCTURE_POWERMODE_PERFORMANCE_MODE;
    powerConfig.dcvsV3Config.setSleepLatency = 1;
    powerConfig.dcvsV3Config.setBusParams    = 1;
    powerConfig.dcvsV3Config.setCoreParams   = 1;
    powerConfig.dcvsV3Config.sleepDisable    = 0;
    powerConfig.dcvsV3Config.setSleepDisable = 0;

    if (perfProfile == "burst") {
        QNN_DEBUG("boostPerformance::perfProfile=burst");
        powerConfig.dcvsV3Config.sleepLatency            = sg_lowerLatency; // set dsp sleep latency ranges 10-65535 micro sec, refer hexagon sdk;
        powerConfig.dcvsV3Config.busVoltageCornerMin     = DCVS_VOLTAGE_VCORNER_MAX_VOLTAGE_CORNER;
        powerConfig.dcvsV3Config.busVoltageCornerTarget  = DCVS_VOLTAGE_VCORNER_MAX_VOLTAGE_CORNER;
        powerConfig.dcvsV3Config.busVoltageCornerMax     = DCVS_VOLTAGE_VCORNER_MAX_VOLTAGE_CORNER;
        powerConfig.dcvsV3Config.coreVoltageCornerMin    = DCVS_VOLTAGE_VCORNER_MAX_VOLTAGE_CORNER;
        powerConfig.dcvsV3Config.coreVoltageCornerTarget = DCVS_VOLTAGE_VCORNER_MAX_VOLTAGE_CORNER;
        powerConfig.dcvsV3Config.coreVoltageCornerMax    = DCVS_VOLTAGE_VCORNER_MAX_VOLTAGE_CORNER;
    }
    else if(perfProfile == "high_performance") {
        QNN_DEBUG("boostPerformance::perfProfile=high_performance");
        powerConfig.dcvsV3Config.sleepLatency            = sg_lowLatency;
        powerConfig.dcvsV3Config.busVoltageCornerMin     = DCVS_VOLTAGE_VCORNER_TURBO;
        powerConfig.dcvsV3Config.busVoltageCornerTarget  = DCVS_VOLTAGE_VCORNER_TURBO;
        powerConfig.dcvsV3Config.busVoltageCornerMax     = DCVS_VOLTAGE_VCORNER_TURBO;
        powerConfig.dcvsV3Config.coreVoltageCornerMin    = DCVS_VOLTAGE_VCORNER_TURBO;
        powerConfig.dcvsV3Config.coreVoltageCornerTarget = DCVS_VOLTAGE_VCORNER_TURBO;
        powerConfig.dcvsV3Config.coreVoltageCornerMax    = DCVS_VOLTAGE_VCORNER_TURBO;
    }
    else {
        QNN_ERROR("Invalid performance profile %s to set power configs", perfProfile.c_str());
        return false;
    }
    
    // Set power config with different performance parameters
    const QnnHtpPerfInfrastructure_PowerConfig_t* powerConfigs[] = { &powerConfig, NULL };
    if (QNN_SUCCESS != perfInfra.setPowerConfig(getPowerConfigId(), powerConfigs)) {
        QNN_ERROR("Failure in setPowerConfig() from boostPerformance");
        return false;
    }

    return disableDcvs(perfInfra);
}

bool resetPerformance(QnnHtpDevice_PerfInfrastructure_t perfInfra) {
    // Initialize the power config and select the voltage corner values for the performance setting.
    QnnHtpPerfInfrastructure_PowerConfig_t powerConfig;
    memset(&powerConfig, 0, sizeof(powerConfig));

    QNN_INF("PERF::resetPerformance");

    powerConfig.option                       = QNN_HTP_PERF_INFRASTRUCTURE_POWER_CONFIGOPTION_DCVS_V3;
    powerConfig.dcvsV3Config.dcvsEnable      = 1;
    powerConfig.dcvsV3Config.setDcvsEnable   = 1;
    powerConfig.dcvsV3Config.contextId       = getPowerConfigId();
    powerConfig.dcvsV3Config.sleepLatency    = sg_highLatency;
    powerConfig.dcvsV3Config.setSleepLatency = 1;
    powerConfig.dcvsV3Config.sleepDisable    = 0;
    powerConfig.dcvsV3Config.setSleepDisable = 0;
    powerConfig.dcvsV3Config.powerMode       = QNN_HTP_PERF_INFRASTRUCTURE_POWERMODE_POWER_SAVER_MODE;
    powerConfig.dcvsV3Config.busVoltageCornerMin     = DCVS_VOLTAGE_VCORNER_MIN_VOLTAGE_CORNER;
    powerConfig.dcvsV3Config.busVoltageCornerTarget  = DCVS_VOLTAGE_VCORNER_MIN_VOLTAGE_CORNER;
    powerConfig.dcvsV3Config.busVoltageCornerMax     = DCVS_VOLTAGE_VCORNER_MIN_VOLTAGE_CORNER;
    powerConfig.dcvsV3Config.setBusParams            = 1;
    powerConfig.dcvsV3Config.coreVoltageCornerMin    = DCVS_VOLTAGE_VCORNER_MIN_VOLTAGE_CORNER;
    powerConfig.dcvsV3Config.coreVoltageCornerTarget = DCVS_VOLTAGE_VCORNER_MIN_VOLTAGE_CORNER;
    powerConfig.dcvsV3Config.coreVoltageCornerMax    = DCVS_VOLTAGE_VCORNER_MIN_VOLTAGE_CORNER;
    powerConfig.dcvsV3Config.setCoreParams           = 1;

    // Set power config with different performance parameters
    const QnnHtpPerfInfrastructure_PowerConfig_t* powerConfigs[] = { &powerConfig, NULL };
    if (QNN_SUCCESS != perfInfra.setPowerConfig(getPowerConfigId(), powerConfigs)) {
        QNN_ERROR("Failure in setPowerConfig() from resetPerformance");
        return false;
    }

    return enableDcvs(perfInfra);
}


// Default path where the outputs will be stored if outputPath is
// not supplied.
const std::string sample_app::QnnSampleApp::s_defaultOutputPath = "./output/";

sample_app::QnnSampleApp::QnnSampleApp(QnnFunctionPointers qnnFunctionPointers,
                                       std::string inputListPaths,
                                       std::string opPackagePaths,
                                       void* backendLibraryHandle,
                                       std::string outputPath,
                                       bool debug,
                                       iotensor::OutputDataType outputDataType,
                                       iotensor::InputDataType inputDataType,
                                       sample_app::ProfilingLevel profilingLevel,
                                       bool dumpOutputs,
                                       std::string cachedBinaryPath,
                                       std::string saveBinaryName, 
                                       const std::vector<LoraAdapter>& lora_adapters,
									                     std::string dlcPath)
    : m_qnnFunctionPointers(qnnFunctionPointers),
      m_outputPath(outputPath),
      m_saveBinaryName(saveBinaryName),
      m_cachedBinaryPath(cachedBinaryPath),
      m_lora_adapters(lora_adapters),
      m_debug(debug),
      m_outputDataType(outputDataType),
      m_inputDataType(inputDataType),
      m_profilingLevel(profilingLevel),
      m_dumpOutputs(dumpOutputs),
      m_isBackendInitialized(false),
      m_isContextCreated(false),
	    m_dlcPath(dlcPath)
{

  split(m_inputListPaths, inputListPaths, ',');
  split(m_opPackagePaths, opPackagePaths, ',');
  if(!dlcPath.empty()){
    size_t pos = dlcPath.find_last_of("\\/"); 
    m_outputPath = dlcPath.substr(0, pos);
    //printf("m_outputPath=%s\n", m_outputPath.c_str());
  }
  if (m_outputPath.empty()) {
    m_outputPath = s_defaultOutputPath;
  }

  if (!m_cachedBinaryPath.empty()) {
      m_runInCpu = false;   // Run *.bin in HTP.
      QNN_DEBUG("Run model on HTP.");
  }

  return;
}

sample_app::QnnSampleApp::~QnnSampleApp() {
  // Free DLC resources using utility function
  dlc_utils::freeDlcResources(m_qnnFunctionPointers.qnnSystemInterfaceHandle,
                              m_dlcHandle,
                              m_dlcLogHandle);
  // Free Profiling object if it was created
  if (nullptr != m_profileBackendHandle) {
    QNN_DEBUG("Freeing backend profile object.");
    if (QNN_PROFILE_NO_ERROR !=
        m_qnnFunctionPointers.qnnInterface.profileFree(m_profileBackendHandle)) {
      QNN_ERROR("Could not free backend profile handle.");
    }
  }
  // Free context if not already done
  if (m_isContextCreated) {
    QNN_DEBUG("Freeing context");
    if (QNN_CONTEXT_NO_ERROR !=
        m_qnnFunctionPointers.qnnInterface.contextFree(m_context, nullptr)) {
      QNN_ERROR("Could not free context");
    }
  }
  m_isContextCreated = false;
  // Terminate backend
  if (m_isBackendInitialized && nullptr != m_qnnFunctionPointers.qnnInterface.backendFree) {
    QNN_DEBUG("Freeing backend");
    if (QNN_BACKEND_NO_ERROR != m_qnnFunctionPointers.qnnInterface.backendFree(m_backendHandle)) {
      QNN_ERROR("Could not free backend");
    }
  }
  m_isBackendInitialized = false;
  // Terminate logging in the backend
  if (nullptr != m_qnnFunctionPointers.qnnInterface.logFree && nullptr != m_logHandle) {
    if (QNN_SUCCESS != m_qnnFunctionPointers.qnnInterface.logFree(m_logHandle)) {
      QNN_WARN("Unable to terminate logging in the backend.");
    }
  }
  return;
}

std::string sample_app::QnnSampleApp::getBackendBuildId() {
  char* backendBuildId{nullptr};
  if (QNN_SUCCESS !=
      m_qnnFunctionPointers.qnnInterface.backendGetBuildId((const char**)&backendBuildId)) {
    QNN_ERROR("Unable to get build Id from the backend.");
  }
  return (backendBuildId == nullptr ? std::string("") : std::string(backendBuildId));
}

// Initialize QnnSampleApp. Things it does:
//  1. Create output directory
//  2. Read all input list paths provided
//      during creation.
sample_app::StatusCode sample_app::QnnSampleApp::initialize() {
  // Create Output Directory
#ifndef __hexagon__
  if (m_dumpOutputs && !::pal::FileOp::checkFileExists(m_outputPath) &&
      !pal::Directory::makePath(m_outputPath)) {
    exitWithMessage("Could not create output directory: " + m_outputPath, EXIT_FAILURE);
  }
#endif
  // Read Input File List
  bool readSuccess;
  std::tie(m_inputFileLists, m_inputNameToIndex, readSuccess) = readInputLists(m_inputListPaths);
  if (!readSuccess) {
    exitWithMessage("Could not read input lists", EXIT_FAILURE);
  }
  // initialize logging in the backend
  if (log::isLogInitialized()) {
    auto logCallback = log::getLogCallback();
    auto logLevel    = log::getLogLevel();
    QNN_INFO("Initializing logging in the backend. Callback: [%p], Log Level: [%d]",
             logCallback,
             logLevel);
    if (QNN_SUCCESS !=
        m_qnnFunctionPointers.qnnInterface.logCreate(logCallback, logLevel, &m_logHandle)) {
      QNN_WARN("Unable to initialize logging in the backend.");
    }
  } else {
    QNN_WARN("Logging not available in the backend.");
  }
  return StatusCode::SUCCESS;
}

sample_app::StatusCode sample_app::QnnSampleApp::initializeProfiling() {
  if (ProfilingLevel::OFF != m_profilingLevel) {
    QNN_INFO("Profiling turned on; level = %d", m_profilingLevel);
    if (ProfilingLevel::BASIC == m_profilingLevel) {
      QNN_INFO("Basic profiling requested. Creating Qnn Profile object.");
      if (QNN_PROFILE_NO_ERROR !=
          m_qnnFunctionPointers.qnnInterface.profileCreate(
              m_backendHandle, QNN_PROFILE_LEVEL_BASIC, &m_profileBackendHandle)) {
        QNN_WARN("Unable to create profile handle in the backend.");
        return StatusCode::FAILURE;
      }
    } else if (ProfilingLevel::DETAILED == m_profilingLevel) {
      QNN_INFO("Detailed profiling requested. Creating Qnn Profile object.");
      if (QNN_PROFILE_NO_ERROR !=
          m_qnnFunctionPointers.qnnInterface.profileCreate(
              m_backendHandle, QNN_PROFILE_LEVEL_DETAILED, &m_profileBackendHandle)) {
        QNN_ERROR("Unable to create profile handle in the backend.");
        return StatusCode::FAILURE;
      }
    }
  }
  return StatusCode::SUCCESS;
}

// Simple method to report error from app to lib.
int32_t sample_app::QnnSampleApp::reportError(const std::string& err) {
  QNN_ERROR("%s", err.c_str());
  return EXIT_FAILURE;
}

// Initialize a QnnBackend.
sample_app::StatusCode sample_app::QnnSampleApp::initializeBackend() {
  auto qnnStatus = m_qnnFunctionPointers.qnnInterface.backendCreate(
      m_logHandle, (const QnnBackend_Config_t**)m_backendConfig, &m_backendHandle);
  if (QNN_BACKEND_NO_ERROR != qnnStatus) {
    QNN_ERROR("Could not initialize backend due to error = %d", qnnStatus);
    return StatusCode::FAILURE;
  }
  QNN_INFO("Initialize Backend Returned Status = %d", qnnStatus);
  m_isBackendInitialized = true;
  return StatusCode::SUCCESS;
}

// Terminate the backend after done.
sample_app::StatusCode sample_app::QnnSampleApp::terminateBackend() {
  if ((m_isBackendInitialized && nullptr != m_qnnFunctionPointers.qnnInterface.backendFree) &&
      QNN_BACKEND_NO_ERROR != m_qnnFunctionPointers.qnnInterface.backendFree(m_backendHandle)) {
    QNN_ERROR("Could not terminate backend");
    return StatusCode::FAILURE;
  }
  m_isBackendInitialized = false;
  return StatusCode::SUCCESS;
}

// Register op packages and interface providers supplied during
// object creation. If there are multiple op packages, register
// them sequentially in the order provided.
sample_app::StatusCode sample_app::QnnSampleApp::registerOpPackages() {
  const size_t pathIdx              = 0;
  const size_t interfaceProviderIdx = 1;
  for (auto const& opPackagePath : m_opPackagePaths) {
    std::vector<std::string> opPackage;
    split(opPackage, opPackagePath, ':');
    QNN_DEBUG("opPackagePath: %s", opPackagePath.c_str());
    const char* target     = nullptr;
    const size_t targetIdx = 2;
    if (opPackage.size() != 2 && opPackage.size() != 3) {
      QNN_ERROR("Malformed opPackageString provided: %s", opPackagePath.c_str());
      return StatusCode::FAILURE;
    }
    if (opPackage.size() == 3) {
      target = (char*)opPackage[targetIdx].c_str();
    }
    if (nullptr == m_qnnFunctionPointers.qnnInterface.backendRegisterOpPackage) {
      QNN_ERROR("backendRegisterOpPackageFnHandle is nullptr.");
      return StatusCode::FAILURE;
    }
    if (QNN_BACKEND_NO_ERROR != m_qnnFunctionPointers.qnnInterface.backendRegisterOpPackage(
                                    m_backendHandle,
                                    (char*)opPackage[pathIdx].c_str(),
                                    (char*)opPackage[interfaceProviderIdx].c_str(),
                                    target)) {
      QNN_ERROR("Could not register Op Package: %s and interface provider: %s",
                opPackage[pathIdx].c_str(),
                opPackage[interfaceProviderIdx].c_str());
      return StatusCode::FAILURE;
    }
    QNN_INFO("Registered Op Package: %s and interface provider: %s",
             opPackage[pathIdx].c_str(),
             opPackage[interfaceProviderIdx].c_str());
  }
  return StatusCode::SUCCESS;
}

// Create a Context in a backend.
sample_app::StatusCode sample_app::QnnSampleApp::createContext() {
  if (QNN_CONTEXT_NO_ERROR !=
      m_qnnFunctionPointers.qnnInterface.contextCreate(m_backendHandle,
                                                       m_deviceHandle,
                                                       (const QnnContext_Config_t**)m_contextConfig,
                                                       &m_context)) {
    QNN_ERROR("Could not create context");
    return StatusCode::FAILURE;
  }
  m_isContextCreated = true;
  return StatusCode::SUCCESS;
}

// Free context after done.
sample_app::StatusCode sample_app::QnnSampleApp::freeContext() {
  // clear graph info first
  if (m_graphsInfo) {
    for (uint32_t gIdx = 0; gIdx < m_graphsCount; gIdx++) {
      if (m_graphsInfo[gIdx]) {
        if (nullptr != m_graphsInfo[gIdx]->graphName) {
          free(m_graphsInfo[gIdx]->graphName);
          m_graphsInfo[gIdx]->graphName = nullptr;
        }
        qnn_wrapper_api::freeQnnTensors(m_graphsInfo[gIdx]->inputTensors,
                                        m_graphsInfo[gIdx]->numInputTensors);
        qnn_wrapper_api::freeQnnTensors(m_graphsInfo[gIdx]->outputTensors,
                                        m_graphsInfo[gIdx]->numOutputTensors);
      }
    }
    free(*m_graphsInfo);
  }
  free(m_graphsInfo);
  m_graphsInfo = nullptr;

  if (QNN_CONTEXT_NO_ERROR !=
      m_qnnFunctionPointers.qnnInterface.contextFree(m_context, m_profileBackendHandle)) {
    QNN_ERROR("Could not free context");
    return StatusCode::FAILURE;
  }

  if (ProfilingLevel::OFF != m_profilingLevel) {
    extractBackendProfilingInfo(m_profileBackendHandle);
  }
  m_isContextCreated = false;
  return StatusCode::SUCCESS;
}

// Calls composeGraph function in QNN's model.so.
// composeGraphs is supposed to populate graph related
// information in m_graphsInfo and m_graphsCount.
// m_debug is the option supplied to composeGraphs to
// say that all intermediate tensors including output tensors
// are expected to be read by the app.
sample_app::StatusCode sample_app::QnnSampleApp::composeGraphs() {
  auto returnStatus = StatusCode::SUCCESS;
  // If DLC path is provided, use DLC-based composition
  if (!m_dlcPath.empty()) {
    printf("DLC path provided, using DLC-based graph composition\n");
    returnStatus = composeGraphsFromDlc();
    return returnStatus;
  } else{
    // Default: compose with QNN's model.so
    // Default path: use model.so
    QNN_INFO("Using model.so for graph composition");
    if (qnn_wrapper_api::ModelError_t::MODEL_NO_ERROR !=
        m_qnnFunctionPointers.composeGraphsFnHandle(
                                m_backendHandle,
                                m_qnnFunctionPointers.qnnInterface,
                                m_context,
                                (const qnn_wrapper_api::GraphConfigInfo_t**)m_graphConfigsInfo,
                                m_graphConfigsInfoCount,
                                &m_graphsInfo,
                                &m_graphsCount,
                                m_debug,
                                log::getLogCallback(),
                                log::getLogLevel())) {
      QNN_ERROR("Failed in composeGraphs()");
      returnStatus = StatusCode::FAILURE;
    }
  }
  return returnStatus;
}

sample_app::StatusCode sample_app::QnnSampleApp::finalizeGraphs() {
  for (size_t graphIdx = 0; graphIdx < m_graphsCount; graphIdx++) {
    if (QNN_GRAPH_NO_ERROR !=
        m_qnnFunctionPointers.qnnInterface.graphFinalize(
            (*m_graphsInfo)[graphIdx].graph, m_profileBackendHandle, nullptr)) {
      return StatusCode::FAILURE;
    }
  }
  if (ProfilingLevel::OFF != m_profilingLevel) {
    extractBackendProfilingInfo(m_profileBackendHandle);
  }
  auto returnStatus = StatusCode::SUCCESS;
  if (!m_saveBinaryName.empty()) {
    QNN_INFO("Before saveBinary(): saving context and metadata.");
    returnStatus = saveBinary();
  } else {
    QNN_DEBUG("m_saveBinaryName is empty()");
  }
  return returnStatus;
}

sample_app::StatusCode sample_app::QnnSampleApp::contextApplyBinarySection(QnnContext_SectionType_t section) {
    sample_app::StatusCode returnStatus = sample_app::StatusCode::SUCCESS;
      for(auto loraadapter = m_lora_adapters.begin(); loraadapter != m_lora_adapters.end(); ++loraadapter){
        std::string model_name = loraadapter->m_graph_name;  
        std::vector<std::string> bin_paths = loraadapter->m_bin_paths;  

        for (std::string binaryUpdatesPath : bin_paths){
          m_profilingOption = ProfilingOption::NONE;   
          returnStatus = applyBinarySection(
              model_name, binaryUpdatesPath, section, m_useMmap,
              m_profilingLevel, m_profilingOption);

          if (returnStatus != sample_app::StatusCode::SUCCESS) {
              QNN_ERROR("Error during call to applyBinarySection.");
              return returnStatus;
          }
        }
    }
  
    return returnStatus;
}

sample_app::StatusCode sample_app::QnnSampleApp::applyBinarySection(
    std::string graphName,
    std::string binaryPath,
    QnnContext_SectionType_t sectionType,
    bool useMmap,
    ProfilingLevel profilingLevel,
    ProfilingOption profilingOption) {

    QNN_FUNCTION_ENTRY_LOG;
    // Get Graph handle
    if (m_qnnFunctionPointers.qnnInterface.propertyHasCapability(QNN_PROPERTY_CONTEXT_SUPPORT_BINARY_UPDATES) !=
        QNN_PROPERTY_SUPPORTED) {
        QNN_ERROR("Backend does not support updates to context binary.");
        return sample_app::StatusCode::FAILURE;
    }
    Qnn_GraphHandle_t graphHandle{ nullptr };
    for (size_t graphIdx = 0; graphIdx < m_graphInfoPtrList.size(); graphIdx++) {
        auto graphInfo = *(m_graphInfoPtrList[graphIdx]);
        if (strcmp(graphInfo.graphName, graphName.c_str()) == 0) {
            graphHandle = graphInfo.graph;
            break;
        }
    }
    if (graphHandle == nullptr) {
        QNN_ERROR("Unable to find the graph with name = %s", graphName.c_str());
        return sample_app::StatusCode::FAILURE;
    }

    uint64_t bufferSize{ 0 };
    // Read serialized binary into a byte buffer
    tools::datautil::StatusCode status{ tools::datautil::StatusCode::SUCCESS };
    std::tie(status, bufferSize) = tools::datautil::getFileSize(binaryPath);
    if (0 == bufferSize) {
        QNN_ERROR("Received path to an empty file. Nothing to deserialize.");
        return StatusCode::FAILURE;
    }

    std::unique_ptr<uint8_t[]> buffer;
    uint8_t* bufferPtr;

#ifdef QNN_ENABLE_MMAP
    std::unique_ptr<mmapped::File> mmappedFile{ nullptr };
#endif

    try {
#ifdef QNN_ENABLE_MMAP
        if (!useMmap) {
#endif
            buffer = std::unique_ptr<uint8_t[]>(new uint8_t[bufferSize]);
            bufferPtr = buffer.get();

            status = tools::datautil::readBinaryFromFile(binaryPath, bufferPtr, bufferSize);
            if (status != tools::datautil::StatusCode::SUCCESS) {
                QNN_ERROR("Failed to read binary data.");
                return StatusCode::FAILURE;
            }
#ifdef QNN_ENABLE_MMAP
        }
        else {
            QNN_VERBOSE("Using mmap for loading the cached binary file at %s", binaryPath.c_str());
            mmappedFile = std::unique_ptr<mmapped::File>(new mmapped::File(binaryPath, false));
            bufferPtr = mmappedFile->data();
        }
#endif
    }
    catch (std::bad_alloc&) {
        QNN_ERROR("Failed to allocate memory.");
        return StatusCode::FAILURE;
    }

    void* voidBufferPtr = static_cast<void*>(bufferPtr);
    QnnContext_Buffer_t contextBuffer{ QNN_CONTEXT_BUFFER_VERSION_1,
                                      {QNN_CONTEXTMEMTYPE_RAW, {{voidBufferPtr, bufferSize}}} };

    // Get Profile Handle
    Qnn_ProfileHandle_t profileBackendHandle{ nullptr };
    bool isProfileHandleCreated = false;
    if (profilingLevel != ProfilingLevel::OFF && profilingLevel != ProfilingLevel::CLIENT &&
        sample_app::StatusCode::SUCCESS == initializeProfileHandle(&m_qnnFunctionPointers.qnnInterface,
            profilingLevel,
            &profileBackendHandle,
            m_numMaxEvents)) {
        isProfileHandleCreated = true;
    }
    if (profilingOption != ProfilingOption::NONE && isProfileHandleCreated) {
        if (sample_app::StatusCode::SUCCESS != initializeProfileConfigOption(
            &m_qnnFunctionPointers.qnnInterface, profilingOption, profileBackendHandle)) {
            QNN_ERROR("Unable to set Profiling Option: %d", profilingOption);
            return sample_app::StatusCode::FAILURE;
        }
    }

    Qnn_ErrorHandle_t executeStatus = QNN_GRAPH_NO_ERROR;

    // Make API CALL
    // TODO: Re-enable profile + signal. For now use wall clock
    auto start = std::chrono::system_clock::now();
    executeStatus = m_qnnFunctionPointers.qnnInterface.contextApplyBinarySection(
        m_context, graphHandle, sectionType, &contextBuffer, nullptr, nullptr);

    auto end = std::chrono::system_clock::now();
    auto elapsed = end - start;
    QNN_VERBOSE("Updated binary with an approximate time %u", elapsed.count());

    if (QNN_GET_ERROR_CODE(executeStatus) != QNN_SUCCESS) {
        if (QNN_GET_ERROR_CODE(executeStatus) == QNN_CONTEXT_ERROR_UNSUPPORTED_FEATURE) {
            QNN_ERROR("Backend does not support application of binary section.");
        }
        else if (QNN_GET_ERROR_CODE(executeStatus) == QNN_CONTEXT_ERROR_MEM_ALLOC) {
            QNN_ERROR("Memory allocation error while creating context update.");
        }
        return sample_app::StatusCode::FAILURE;
    }
    else {
        QNN_VERBOSE("Binary section retrieved from %s and applied to graph %s.\n",
            binaryPath.c_str(),
            graphName.c_str());
    }

    if (isProfileHandleCreated) {
        terminateProfileHandle(&m_qnnFunctionPointers.qnnInterface, profileBackendHandle);
    }
    QNN_FUNCTION_EXIT_LOG;
    return sample_app::StatusCode::SUCCESS;
}

sample_app::StatusCode sample_app::QnnSampleApp::initializeProfileHandle(
    const QNN_INTERFACE_VER_TYPE* qnnInterfaceHandle,
    ProfilingLevel profilingLevel,
    Qnn_ProfileHandle_t* profileHandle,
    const uint64_t numMaxEvents) {
    QNN_FUNCTION_ENTRY_LOG;
    if (nullptr == qnnInterfaceHandle->profileCreate) {
        if (ProfilingLevel::CLIENT != profilingLevel) {
            QNN_ERROR(
                "Profiling not supported by this backend. Cannot capture basic profiling "
                "information from backend. Only net run captured stats are present.");
            return StatusCode::FAILURE;
        }
        else {
            return StatusCode::SUCCESS;
        }
    }

    if (ProfilingLevel::BASIC == profilingLevel) {
        auto qnnStatus =
            qnnInterfaceHandle->profileCreate(m_backendHandle, QNN_PROFILE_LEVEL_BASIC, profileHandle);
        if (QNN_PROFILE_NO_ERROR != qnnStatus) {
            QNN_ERROR(
                "Unable to create Basic profile handle in the backend.");
            return verifyFailReturnStatus(qnnStatus);
        }
    }
    else {
        auto qnnStatus = qnnInterfaceHandle->profileCreate(
            m_backendHandle, QNN_PROFILE_LEVEL_DETAILED, profileHandle);
        if (QNN_PROFILE_NO_ERROR != qnnStatus) {
            QNN_ERROR(
                "Unable to create Detailed profile handle in the backend.");
            return verifyFailReturnStatus(qnnStatus);
        }
    }

    if (numMaxEvents != std::numeric_limits<uint64_t>::max()) {
        if (qnnInterfaceHandle->propertyHasCapability(QNN_PROPERTY_PROFILE_SUPPORT_MAX_EVENTS_CONFIG) ==
            QNN_PROPERTY_SUPPORTED) {
            QnnProfile_Config_t numMaxEventsConfig = QNN_PROFILE_CONFIG_INIT;
            numMaxEventsConfig.option = QNN_PROFILE_CONFIG_OPTION_MAX_EVENTS;
            numMaxEventsConfig.numMaxEvents = numMaxEvents;
            const QnnProfile_Config_t* profileConfigs[] = { &numMaxEventsConfig, nullptr };
            Qnn_ErrorHandle_t qnnProfileError =
                qnnInterfaceHandle->profileSetConfig(*profileHandle, profileConfigs);
            if (qnnProfileError != QNN_PROFILE_NO_ERROR) {
                QNN_ERROR(
                    "Failed to set profile config option: %d, with error: %d",
                    numMaxEventsConfig.option,
                    qnnProfileError);
                return StatusCode::FAILURE;
            }
        }
        else {
            QNN_ERROR("Unsupported profile config supplied: set number of max profiling events.");
            return StatusCode::FAILURE;
        }
    }

    QNN_FUNCTION_EXIT_LOG;
    return StatusCode::SUCCESS;
}

bool sample_app::QnnSampleApp::binaryUpdates() {
    return m_lora_adapters.size() > 0;
}

void sample_app::QnnSampleApp::update_m_lora_adapters(std::vector<LoraAdapter>& lora_adapters) {
    m_lora_adapters = lora_adapters;
}

sample_app::StatusCode sample_app::QnnSampleApp::initializeProfileConfigOption(
    const QNN_INTERFACE_VER_TYPE* qnnInterfaceHandle,
    ProfilingOption profilingOption,
    Qnn_ProfileHandle_t profileHandle) {
    QNN_FUNCTION_ENTRY_LOG;
    sample_app::StatusCode returnStatus;
    QnnProfile_Config_t optraceConfig = QNN_PROFILE_CONFIG_INIT;
    if (profilingOption == sample_app::ProfilingOption::OPTRACE) {
        optraceConfig.option = QNN_PROFILE_CONFIG_OPTION_ENABLE_OPTRACE;
        optraceConfig.enableOptrace = true;
        const QnnProfile_Config_t* profileConfigs[] = { &optraceConfig, nullptr };
        Qnn_ErrorHandle_t qnnStatus =
            qnnInterfaceHandle->profileSetConfig(profileHandle, profileConfigs);
        QNN_ERROR(
            "Failed to set profile config option: %d, with error: %d",
            profilingOption,
            qnnStatus);
        returnStatus = verifyFailReturnStatus(qnnStatus);

    }
    else {
        QNN_ERROR("Unknown config option: %d", profilingOption);
        returnStatus = sample_app::StatusCode::FAILURE;
    }

    QNN_FUNCTION_EXIT_LOG;
    return returnStatus;
}

sample_app::StatusCode sample_app::QnnSampleApp::terminateProfileHandle(
    const QNN_INTERFACE_VER_TYPE* qnnInterfaceHandle, Qnn_ProfileHandle_t profileHandle) {
    QNN_FUNCTION_ENTRY_LOG;
    auto returnStatus = sample_app::StatusCode::SUCCESS;
    if (nullptr != profileHandle && nullptr != qnnInterfaceHandle->profileFree) {
        QNN_DEBUG("Freeing backend profile object.");
        auto result = qnnInterfaceHandle->profileFree(profileHandle);
        QNN_ERROR("Could not free backend profile handle.");
        returnStatus = verifyFailReturnStatus(result);
    }
    QNN_FUNCTION_EXIT_LOG;
    return returnStatus;
}

sample_app::StatusCode sample_app::QnnSampleApp::addGraphToContext(
    qnn_wrapper_api::GraphInfo_t* graphInfo) {
    QNN_FUNCTION_ENTRY_LOG;
    m_graphInfoPtrList.push_back(graphInfo);
    QNN_FUNCTION_EXIT_LOG;
    return StatusCode::SUCCESS;
}

sample_app::StatusCode sample_app::QnnSampleApp::addGraphsToContext(
    qnn_wrapper_api::GraphInfo_t** graphInfos, uint32_t numGraphs) {
    QNN_FUNCTION_ENTRY_LOG;
    for (uint32_t i = 0; i < numGraphs; i++) {
        m_graphInfoPtrList.push_back(graphInfos[i]);
    }
    QNN_FUNCTION_EXIT_LOG;
    return StatusCode::SUCCESS;
}

sample_app::StatusCode sample_app::QnnSampleApp::createFromBinary() {
  QNN_FUNCTION_ENTRY_LOG;
  if (m_cachedBinaryPath.empty()) {
    QNN_ERROR("No name provided to read binary file from.");
    return StatusCode::FAILURE;
  }
  if (nullptr == m_qnnFunctionPointers.qnnSystemInterface.systemContextCreate ||
      nullptr == m_qnnFunctionPointers.qnnSystemInterface.systemContextGetBinaryInfo ||
      nullptr == m_qnnFunctionPointers.qnnSystemInterface.systemContextFree) {
    QNN_ERROR("QNN System function pointers are not populated.");
    return StatusCode::FAILURE;
  }
  uint64_t bufferSize{0};

  // TimerHelper timerHelper;

  tools::datautil::StatusCode status{tools::datautil::StatusCode::SUCCESS};
  std::tie(status, bufferSize) = tools::datautil::getFileSize(m_cachedBinaryPath);
  if (0 == bufferSize) {
    QNN_ERROR("Received path to an empty file. Nothing to deserialize.");
    return StatusCode::FAILURE;
  }

#ifdef _WIN32
#define MMAP_FILE
#endif

#ifdef MMAP_FILE
  HANDLE hFile = CreateFile(m_cachedBinaryPath.c_str(), GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
  if (hFile == INVALID_HANDLE_VALUE) {
    printf("Failed to open file %s. err: %s", m_cachedBinaryPath.c_str(), strerror(errno));
    return StatusCode::FAILURE;
  }

  // Ensure handles are closed on all paths.
  HANDLE hFileMap = NULL;
  uint8_t *buffer  = NULL;
  auto cleanupWinMmap = [&]() {
    if (buffer) {
      UnmapViewOfFile(buffer);
      buffer = NULL;
    }
    if (hFileMap) {
      CloseHandle(hFileMap);
      hFileMap = NULL;
    }
    if (hFile != INVALID_HANDLE_VALUE) {
      CloseHandle(hFile);
      hFile = INVALID_HANDLE_VALUE;
    }
  };

  hFileMap = CreateFileMapping(hFile, NULL, PAGE_READONLY, 0, (DWORD)bufferSize, NULL);

  if (hFileMap == NULL) {
    printf("Failed to create file mapping of file %s. err: %s", m_cachedBinaryPath.c_str(), strerror(errno));
    cleanupWinMmap();
    return StatusCode::FAILURE;
  }

  buffer = (uint8_t *)MapViewOfFile(hFileMap, FILE_MAP_READ, 0, 0, 0);

  if (buffer == NULL) {
    printf("MapViewOfFile fail %s. err: %s", m_cachedBinaryPath.c_str(), strerror(errno));
    cleanupWinMmap();
    return StatusCode::FAILURE;
  }
#else
  std::shared_ptr<uint8_t> buffer{nullptr};
  // read serialized binary into a byte buffer

  buffer = std::shared_ptr<uint8_t>(new uint8_t[bufferSize], std::default_delete<uint8_t[]>());
  if (!buffer) {
    QNN_ERROR("Failed to allocate memory.");
    return StatusCode::FAILURE;
  }

  status = tools::datautil::readBinaryFromFile(
      m_cachedBinaryPath, reinterpret_cast<uint8_t*>(buffer.get()), bufferSize);
  if (status != tools::datautil::StatusCode::SUCCESS) {
    QNN_ERROR("Failed to read binary data.");
    return StatusCode::FAILURE;
  }
#endif

  // timerHelper.Print("Read model file to memory.");
  // timerHelper.Reset();

  // inspect binary info
  auto returnStatus = StatusCode::SUCCESS;
  QnnSystemContext_Handle_t sysCtxHandle{nullptr};
  if (QNN_SUCCESS != m_qnnFunctionPointers.qnnSystemInterface.systemContextCreate(&sysCtxHandle)) {
    QNN_ERROR("Could not create system handle.");
    returnStatus = StatusCode::FAILURE;
  }
  const QnnSystemContext_BinaryInfo_t* binaryInfo{nullptr};
  Qnn_ContextBinarySize_t binaryInfoSize{0};
  if (StatusCode::SUCCESS == returnStatus &&
      QNN_SUCCESS != m_qnnFunctionPointers.qnnSystemInterface.systemContextGetBinaryInfo(
                         sysCtxHandle,
#ifdef MMAP_FILE
                         static_cast<void*>(buffer),
#else
                         static_cast<void*>(buffer.get()),
#endif
                         bufferSize,
                         &binaryInfo,
                         &binaryInfoSize)) {
    QNN_ERROR("Failed to get context binary info");
    returnStatus = StatusCode::FAILURE;
  }

  // fill GraphInfo_t based on binary info
  if (StatusCode::SUCCESS == returnStatus &&
      !copyMetadataToGraphsInfo(binaryInfo, m_graphsInfo, m_graphsCount)) {
    QNN_ERROR("Failed to copy metadata.");
    returnStatus = StatusCode::FAILURE;
  }
  m_qnnFunctionPointers.qnnSystemInterface.systemContextFree(sysCtxHandle);
  sysCtxHandle = nullptr;

  if (StatusCode::SUCCESS != addGraphsToContext(m_graphsInfo, m_graphsCount)) {
      QNN_ERROR("Unable to add the retrieved Graphs into ContextWrapper");
      returnStatus = StatusCode::FAILURE;
  }

  if (StatusCode::SUCCESS == returnStatus &&
      nullptr == m_qnnFunctionPointers.qnnInterface.contextCreateFromBinary) {
    QNN_ERROR("contextCreateFromBinaryFnHandle is nullptr.");
    returnStatus = StatusCode::FAILURE;
  }
  if (StatusCode::SUCCESS == returnStatus &&
      m_qnnFunctionPointers.qnnInterface.contextCreateFromBinary(
          m_backendHandle,
          m_deviceHandle,
          (const QnnContext_Config_t**)m_contextConfig,
#ifdef MMAP_FILE
          static_cast<void*>(buffer),
#else
          static_cast<void*>(buffer.get()),
#endif
          bufferSize,
          &m_context,
          m_profileBackendHandle)) {
    QNN_ERROR("Could not create context from binary.");
    returnStatus = StatusCode::FAILURE;
  }
  if (ProfilingLevel::OFF != m_profilingLevel) {
    extractBackendProfilingInfo(m_profileBackendHandle);
  }
  m_isContextCreated = true;
  if (StatusCode::SUCCESS == returnStatus) {
    for (size_t graphIdx = 0; graphIdx < m_graphsCount; graphIdx++) {
      if (nullptr == m_qnnFunctionPointers.qnnInterface.graphRetrieve) {
        QNN_ERROR("graphRetrieveFnHandle is nullptr.");
        returnStatus = StatusCode::FAILURE;
        break;
      }
      if (QNN_SUCCESS !=
          m_qnnFunctionPointers.qnnInterface.graphRetrieve(
              m_context, (*m_graphsInfo)[graphIdx].graphName, &((*m_graphsInfo)[graphIdx].graph))) {
        QNN_ERROR("Unable to retrieve graph handle for graph Idx: %d", graphIdx);
        returnStatus = StatusCode::FAILURE;
      }
    }
  }
  if (StatusCode::SUCCESS != returnStatus) {
    QNN_DEBUG("Cleaning up graph Info structures.");
    qnn_wrapper_api::freeGraphsInfo(&m_graphsInfo, m_graphsCount);
  }

  // timerHelper.Print("contextCreateFromBinary.");
  // timerHelper.Reset();

#ifdef MMAP_FILE
  cleanupWinMmap();
#endif

  // timerHelper.Print("UnmapViewOfFile.");

QNN_FUNCTION_EXIT_LOG;
  return returnStatus;
}

sample_app::StatusCode sample_app::QnnSampleApp::saveBinary() {
  if (m_saveBinaryName.empty()) {
    QNN_ERROR("No name provided to save binary file.");
    return StatusCode::FAILURE;
  }
  if (nullptr == m_qnnFunctionPointers.qnnInterface.contextGetBinarySize ||
      nullptr == m_qnnFunctionPointers.qnnInterface.contextGetBinary) {
    QNN_ERROR("contextGetBinarySizeFnHandle or contextGetBinaryFnHandle is nullptr.");
    return StatusCode::FAILURE;
  }
  uint64_t requiredBufferSize{0};
  if (QNN_CONTEXT_NO_ERROR !=
      m_qnnFunctionPointers.qnnInterface.contextGetBinarySize(m_context, &requiredBufferSize)) {
    QNN_ERROR("Could not get the required binary size.");
    return StatusCode::FAILURE;
  }
  std::unique_ptr<uint8_t[]> saveBuffer(new uint8_t[requiredBufferSize]);
  if (nullptr == saveBuffer) {
    QNN_ERROR("Could not allocate buffer to save binary.");
    return StatusCode::FAILURE;
  }
  uint64_t writtenBufferSize{0};
  if (QNN_CONTEXT_NO_ERROR !=
      m_qnnFunctionPointers.qnnInterface.contextGetBinary(m_context,
                                                          reinterpret_cast<void*>(saveBuffer.get()),
                                                          requiredBufferSize,
                                                          &writtenBufferSize)) {
    QNN_ERROR("Could not get binary.");
    return StatusCode::FAILURE;
  }
  if (requiredBufferSize < writtenBufferSize) {
    QNN_ERROR(
        "Illegal written buffer size [%d] bytes. Cannot exceed allocated memory of [%d] bytes",
        writtenBufferSize,
        requiredBufferSize);
    return StatusCode::FAILURE;
  }
#ifndef __hexagon__
  auto dataUtilStatus = tools::datautil::writeBinaryToFile(
      m_outputPath, m_saveBinaryName + ".bin", (uint8_t*)saveBuffer.get(), writtenBufferSize);
  if (tools::datautil::StatusCode::SUCCESS != dataUtilStatus) {
    QNN_ERROR("Error while writing binary to file.");
    return StatusCode::FAILURE;
  }
#endif
  return StatusCode::SUCCESS;
}

// C:\Qualcomm\AIStack\QNN\<version>\include\QNN\QnnProfile.h
// C:\Qualcomm\AIStack\QNN\<version>\include\QNN\HTP\QnnHtpProfile.h
sample_app::StatusCode sample_app::QnnSampleApp::composeGraphsFromDlc() {
  QNN_DEBUG("Composing graphs from DLC\n");
  // Create DLC handle using utility function
  auto dlcStatus = dlc_utils::createDlcHandle(m_qnnFunctionPointers.qnnSystemInterfaceHandle,
                                              m_dlcPath,
                                              log::getLogCallback(),
                                              log::getLogLevel(),
                                              m_dlcLogHandle,
                                              m_dlcHandle);

  if (dlc_utils::StatusCode::SUCCESS != dlcStatus) {
    QNN_ERROR("Failed to create DLC handle");
    return StatusCode::FAILURE;
  }

  // Compose graphs from DLC using utility function
  dlcStatus = dlc_utils::composeGraphsFromDlc(m_qnnFunctionPointers.qnnSystemInterfaceHandle,
                                              m_dlcHandle,
                                              m_backendHandle,
                                              m_context,
                                              m_qnnFunctionPointers.qnnInterfaceHandle,
                                              m_graphsInfo,
                                              m_graphsCount);

  if (dlc_utils::StatusCode::SUCCESS != dlcStatus) {
    QNN_ERROR("Failed to compose graphs from DLC");
    return StatusCode::FAILURE;
  }

  QNN_INFO("Successfully composed %d graphs from DLC", m_graphsCount);
  return StatusCode::SUCCESS;
}
sample_app::StatusCode sample_app::QnnSampleApp::extractBackendProfilingInfo(
    Qnn_ProfileHandle_t profileHandle) {
  if (nullptr == profileHandle) {
    QNN_ERROR("Backend Profile handle is nullptr; may not be initialized.");
    return StatusCode::FAILURE;
  }
  const QnnProfile_EventId_t* profileEvents{nullptr};
  uint32_t numEvents{0};
  if (QNN_PROFILE_NO_ERROR != m_qnnFunctionPointers.qnnInterface.profileGetEvents(
                                  profileHandle, &profileEvents, &numEvents)) {
    QNN_ERROR("Failure in profile get events.");
    return StatusCode::FAILURE;
  }
  QNN_INFO("ProfileEvents: [%p], numEvents: [%d]", profileEvents, numEvents);
  for (size_t event = 0; event < numEvents; event++) {
    extractProfilingEvent(*(profileEvents + event));
    extractProfilingSubEvents(*(profileEvents + event));
  }
  return StatusCode::SUCCESS;
}

sample_app::StatusCode sample_app::QnnSampleApp::extractProfilingSubEvents(
    QnnProfile_EventId_t profileEventId) {
  const QnnProfile_EventId_t* profileSubEvents{nullptr};
  uint32_t numSubEvents{0};
  if (QNN_PROFILE_NO_ERROR != m_qnnFunctionPointers.qnnInterface.profileGetSubEvents(
                                  profileEventId, &profileSubEvents, &numSubEvents)) {
    QNN_ERROR("Failure in profile get sub events.");
    return StatusCode::FAILURE;
  }
  QNN_INFO("ProfileSubEvents: [%p], numSubEvents: [%d]", profileSubEvents, numSubEvents);
  for (size_t subEvent = 0; subEvent < numSubEvents; subEvent++) {
    extractProfilingEvent(*(profileSubEvents + subEvent));
    extractProfilingSubEvents(*(profileSubEvents + subEvent));
  }
  return StatusCode::SUCCESS;
}

sample_app::StatusCode sample_app::QnnSampleApp::extractProfilingEvent(
    QnnProfile_EventId_t profileEventId) {
  QnnProfile_EventData_t eventData;
  if (QNN_PROFILE_NO_ERROR !=
      m_qnnFunctionPointers.qnnInterface.profileGetEventData(profileEventId, &eventData)) {
    QNN_ERROR("Failure in profile get event type.");
    return StatusCode::FAILURE;
  }
  QNN_INFO("Printing Event Info - Event Type: [%d], Event Value: [%" PRIu64
            "], Event Identifier: [%s], Event Unit: [%d]",
            eventData.type,
            eventData.value,
            eventData.identifier,
            eventData.unit);
  return StatusCode::SUCCESS;
}

sample_app::StatusCode sample_app::QnnSampleApp::verifyFailReturnStatus(Qnn_ErrorHandle_t errCode) {
  auto returnStatus = sample_app::StatusCode::FAILURE;
  switch (errCode) {
    case QNN_COMMON_ERROR_SYSTEM_COMMUNICATION:
      returnStatus = sample_app::StatusCode::FAILURE_SYSTEM_COMMUNICATION_ERROR;
      break;
    case QNN_COMMON_ERROR_SYSTEM:
      returnStatus = sample_app::StatusCode::FAILURE_SYSTEM_ERROR;
      break;
    case QNN_COMMON_ERROR_NOT_SUPPORTED:
      returnStatus = sample_app::StatusCode::QNN_FEATURE_UNSUPPORTED;
      break;
    default:
      break;
  }
  return returnStatus;
}

sample_app::StatusCode sample_app::QnnSampleApp::isDevicePropertySupported() {
  if (nullptr != m_qnnFunctionPointers.qnnInterface.propertyHasCapability) {
    auto qnnStatus =
        m_qnnFunctionPointers.qnnInterface.propertyHasCapability(QNN_PROPERTY_GROUP_DEVICE);
    if (QNN_PROPERTY_NOT_SUPPORTED == qnnStatus) {
      QNN_WARN("Device property is not supported");
    }
    if (QNN_PROPERTY_ERROR_UNKNOWN_KEY == qnnStatus) {
      QNN_ERROR("Device property is not known to backend");
      return StatusCode::FAILURE;
    }
  }
  return StatusCode::SUCCESS;
}

sample_app::StatusCode sample_app::QnnSampleApp::isFinalizeDeserializedGraphSupported() {
  auto returnStatus = StatusCode::FAILURE;
  if (nullptr != m_qnnFunctionPointers.qnnInterface.propertyHasCapability) {
    auto qnnStatus = m_qnnFunctionPointers.qnnInterface.propertyHasCapability(
        QNN_PROPERTY_GRAPH_SUPPORT_FINALIZE_DESERIALIZED_GRAPH);
    if (QNN_PROPERTY_SUPPORTED != qnnStatus) {
      QNN_ERROR("Device property is not supported");
      return returnStatus;
    }
    returnStatus = StatusCode::SUCCESS;
  }
  return returnStatus;
}

sample_app::StatusCode sample_app::QnnSampleApp::createDevice() {
  if (nullptr != m_qnnFunctionPointers.qnnInterface.deviceCreate) {
    auto qnnStatus =
        m_qnnFunctionPointers.qnnInterface.deviceCreate(m_logHandle, nullptr, &m_deviceHandle);
    if (QNN_SUCCESS != qnnStatus && QNN_DEVICE_ERROR_UNSUPPORTED_FEATURE != qnnStatus) {
      QNN_ERROR("Failed to create device");
      return verifyFailReturnStatus(qnnStatus);
    }
  }
  return StatusCode::SUCCESS;
}

sample_app::StatusCode sample_app::QnnSampleApp::freeDevice() {
  if (nullptr != m_qnnFunctionPointers.qnnInterface.deviceFree) {
    auto qnnStatus = m_qnnFunctionPointers.qnnInterface.deviceFree(m_deviceHandle);
    if (QNN_SUCCESS != qnnStatus && QNN_DEVICE_ERROR_UNSUPPORTED_FEATURE != qnnStatus) {
      QNN_ERROR("Failed to free device");
      return verifyFailReturnStatus(qnnStatus);
    }
  }
  return StatusCode::SUCCESS;
}

// executeGraphs() that is currently used by qnn-sample-app's main.cpp.
// This function runs all the graphs present in model.so by reading
// inputs from input_list based files and writes output to .raw files.
sample_app::StatusCode sample_app::QnnSampleApp::executeGraphs() {
  auto returnStatus = StatusCode::SUCCESS;
  for (size_t graphIdx = 0; graphIdx < m_graphsCount; graphIdx++) {
    QNN_DEBUG("Starting execution for graphIdx: %d", graphIdx);
    if (graphIdx >= m_inputFileLists.size()) {
      QNN_ERROR("No Inputs available for: %d", graphIdx);
      returnStatus = StatusCode::FAILURE;
      break;
    }
    Qnn_Tensor_t* inputs  = nullptr;
    Qnn_Tensor_t* outputs = nullptr;
    if (iotensor::StatusCode::SUCCESS !=
        m_ioTensor.setupInputAndOutputTensors(&inputs, &outputs, (*m_graphsInfo)[graphIdx])) {
      QNN_ERROR("Error in setting up Input and output Tensors for graphIdx: %d", graphIdx);
      returnStatus = StatusCode::FAILURE;
      break;
    }
    auto inputFileList = m_inputFileLists[graphIdx];
    auto graphInfo     = (*m_graphsInfo)[graphIdx];
    if (!inputFileList.empty()) {
      size_t totalCount           = inputFileList[0].size();
      size_t inputFileIndexOffset = 0;
      while (inputFileIndexOffset < totalCount) {
        iotensor::StatusCode iotReturnStatus;
        size_t numInputFilesPopulated;
        size_t batchSize;
        std::tie(iotReturnStatus, numInputFilesPopulated, batchSize) =
            m_ioTensor.populateInputTensors(graphIdx,
                                            inputFileList,
                                            inputFileIndexOffset,
                                            false,
                                            m_inputNameToIndex[graphIdx],
                                            inputs,
                                            graphInfo,
                                            m_inputDataType);
        if (iotensor::StatusCode::SUCCESS != iotReturnStatus) {
          returnStatus = StatusCode::FAILURE;
        }
        if (StatusCode::SUCCESS == returnStatus) {
          QNN_DEBUG("Successfully populated input tensors for graphIdx: %d", graphIdx);
          Qnn_ErrorHandle_t executeStatus = QNN_GRAPH_NO_ERROR;
          executeStatus =
              m_qnnFunctionPointers.qnnInterface.graphExecute(graphInfo.graph,
                                                              inputs,
                                                              graphInfo.numInputTensors,
                                                              outputs,
                                                              graphInfo.numOutputTensors,
                                                              m_profileBackendHandle,
                                                              nullptr);
          if (QNN_GRAPH_NO_ERROR != executeStatus) {
            returnStatus = StatusCode::FAILURE;
          }
          if (StatusCode::SUCCESS == returnStatus) {
            QNN_DEBUG("Successfully executed graphIdx: %d ", graphIdx);
#ifndef __hexagon__
            if (iotensor::StatusCode::SUCCESS !=
                m_ioTensor.writeOutputTensors(graphIdx,
                                              inputFileIndexOffset,
                                              graphInfo.graphName,
                                              outputs,
                                              graphInfo.numOutputTensors,
                                              m_outputDataType,
                                              m_graphsCount,
                                              m_outputPath,
                                              numInputFilesPopulated,
                                              batchSize)) {
              returnStatus = StatusCode::FAILURE;
            }
#endif
          }
          inputFileIndexOffset += numInputFilesPopulated;
        }
        if (StatusCode::SUCCESS != returnStatus) {
          QNN_ERROR("Execution of Graph: %d failed!", graphIdx);
          break;
        }
      }
    }
    m_ioTensor.tearDownInputAndOutputTensors(
        inputs, outputs, graphInfo.numInputTensors, graphInfo.numOutputTensors);
    inputs  = nullptr;
    outputs = nullptr;
    if (StatusCode::SUCCESS != returnStatus) {
      break;
    }
  }

  return returnStatus;
}

// #define DEBUG_INFERENCE 1
#ifdef DEBUG_INFERENCE

#define BUFSIZE             (256)

void bufferToFile(std::vector<uint8_t*>& buffers, std::vector<size_t>& size, std::string& data_name) {
    char data_path[BUFSIZE];
    
    QNN_DEBUG("Dump input data, input count = %d", size.size());

    for (int i = 0; i < size.size(); i++) {
        QNN_DEBUG("Dump input data, size = %d", size[i]);

        snprintf(data_path, BUFSIZE, data_name.c_str(), i);
        std::ofstream os(data_path, std::ofstream::binary);
        if (!os) {
            QNN_ERR("Failed to open file for writing: %s", data_path);
        }
        else {
            os.write(reinterpret_cast<char*>(&(*(buffers[i]))), size[i]);
        }
        os.close();
    }
}

// issue#24
void printDataTypes(std::vector<std::string>& vec) {
	std::cout << "[ ";
    for (size_t i = 0; i < vec.size(); ++i) {
        std::cout << vec[i] << " ";
    }
	std::cout << "]\n";
}

void printArrayOfVector(std::vector<std::vector<size_t>> arrayOfVectors){
      for (const auto& vec : arrayOfVectors) {
        std::cout << "[ ";
        for (size_t val : vec) {
            std::cout << val << " ";
        }
        std::cout << "]\n";
    }
}
#endif

// improve performance.
sample_app::StatusCode sample_app::QnnSampleApp::setupInputAndOutputTensors()
{
  auto returnStatus = qnn::tools::iotensor::StatusCode::SUCCESS;

  for (size_t graphIdx = 0; graphIdx < m_graphsCount; graphIdx++) {
    auto& graphInfo = (*m_graphsInfo)[graphIdx];
    Qnn_Tensor_t** inputs  = &(graphInfo.m_inputs );
    Qnn_Tensor_t** outputs = &(graphInfo.m_outputs);
    returnStatus = m_ioTensor.setupInputAndOutputTensors(inputs, outputs, graphInfo);
    if (qnn::tools::iotensor::StatusCode::SUCCESS != returnStatus) {
      QNN_ERROR("Error in setting up Input and output Tensors for graphIdx: %d", graphIdx);
      break;
    }
  }

  return static_cast<sample_app::StatusCode>(returnStatus);
}

// improve performance.
sample_app::StatusCode sample_app::QnnSampleApp::tearDownInputAndOutputTensors()
{
  auto returnStatus = qnn::tools::iotensor::StatusCode::SUCCESS;

  for (size_t graphIdx = 0; graphIdx < m_graphsCount; graphIdx++) {
    auto& graphInfo = (*m_graphsInfo)[graphIdx];
    Qnn_Tensor_t* inputs  = graphInfo.m_inputs ;
    Qnn_Tensor_t* outputs = graphInfo.m_outputs;
    returnStatus = m_ioTensor.tearDownInputAndOutputTensors(inputs, outputs, graphInfo.numInputTensors, graphInfo.numOutputTensors);
    graphInfo.m_inputs  = nullptr;
    graphInfo.m_outputs = nullptr;
    if (qnn::tools::iotensor::StatusCode::SUCCESS != returnStatus) {
      QNN_ERROR("Error in tear down Input and output Tensors for graphIdx: %d", graphIdx);
      break;
    }
  }

  return static_cast<sample_app::StatusCode>(returnStatus);
}

// issue#24
std::string dataTypeToString(Qnn_DataType_t dtype) {
    switch (dtype) {
      case QNN_DATATYPE_INT_8:   return "int8";
      case QNN_DATATYPE_INT_16:   return "int16";
      case QNN_DATATYPE_INT_32:   return "int32";
      case QNN_DATATYPE_INT_64:   return "int64";
      case QNN_DATATYPE_UINT_8:   return "uint8";
      case QNN_DATATYPE_UINT_16:   return "uint16";
      case QNN_DATATYPE_UINT_32:   return "uint32";
      case QNN_DATATYPE_UINT_64:   return "uint64";
      case QNN_DATATYPE_FLOAT_16:   return "float16";
      case QNN_DATATYPE_FLOAT_32:   return "float32";
      case QNN_DATATYPE_FLOAT_64:   return "float64";
      case QNN_DATATYPE_SFIXED_POINT_4:   return "sfp4";
      case QNN_DATATYPE_SFIXED_POINT_8:   return "sfp8";                                                                  
      case QNN_DATATYPE_SFIXED_POINT_16:   return "sfp16";  
      case QNN_DATATYPE_SFIXED_POINT_32:   return "sfp32";  
      case QNN_DATATYPE_UFIXED_POINT_4:   return "ufp4";  
      case QNN_DATATYPE_UFIXED_POINT_8:   return "ufp8";  
      case QNN_DATATYPE_UFIXED_POINT_16:   return "ufp16";  
      case QNN_DATATYPE_UFIXED_POINT_32:   return "ufp32";  
      case QNN_DATATYPE_BOOL_8:   return "bool_";                                      
      case QNN_DATATYPE_STRING:   return "string_"; 
      case QNN_DATATYPE_UNDEFINED:   return "UNDEFINED"; 
      default:  return "UNKNOWN";
    }
}

std::vector<std::vector<size_t>> sample_app::QnnSampleApp::getInputShapes(){
	if(m_inputShapes.empty()){
		auto graphInfo = (*m_graphsInfo)[0/*graphIdx*/];
		Qnn_Tensor_t* inputs  = graphInfo.m_inputs;
		for (size_t inputIdx = 0; inputIdx < graphInfo.numInputTensors; inputIdx++) {
			if (QNN_TENSOR_GET_DIMENSIONS(inputs[inputIdx]) == nullptr || QNN_TENSOR_GET_RANK(inputs[inputIdx]) == 0) {
				//printf("[ERROR] input tensor %zu has nullptr dimensions or rank == 0\n", inputIdx);
				continue;
			}
			std::vector<size_t> dims;
			m_ioTensor.fillDims(dims, QNN_TENSOR_GET_DIMENSIONS(inputs[inputIdx]), QNN_TENSOR_GET_RANK(inputs[inputIdx]));
			m_inputShapes.push_back(dims);
		}
	}
#ifdef DEBUG_INFERENCE
  printf("[DEBUG]m_inputShapes:");
  printArrayOfVector(m_inputShapes);
#endif  
  return m_inputShapes;
}

std::string sample_app::QnnSampleApp::getGraphName(){
	if(m_graphName.empty()){
		auto graphInfo = (*m_graphsInfo)[0/*graphIdx*/];
		m_graphName  = graphInfo.graphName;
	}
    return m_graphName;
}

std::vector<std::string> sample_app::QnnSampleApp::getInputName(){
	if(m_inputName.empty()){
		auto graphInfo = (*m_graphsInfo)[0/*graphIdx*/];
		Qnn_Tensor_t* inputs  = graphInfo.m_inputs;
 		for (size_t inputIdx = 0; inputIdx < graphInfo.numInputTensors; inputIdx++) {
			std::string inputName = QNN_TENSOR_GET_NAME(inputs[inputIdx]);
			m_inputName.push_back(inputName);
		}
    }
	return m_inputName;
}


std::vector<std::string> sample_app::QnnSampleApp::getOutputName(){
	if(m_outputName.empty()){
		auto graphInfo = (*m_graphsInfo)[0/*graphIdx*/];
		Qnn_Tensor_t* outputs  = graphInfo.m_outputs;
		for (size_t outputIdx = 0; outputIdx < graphInfo.numOutputTensors; outputIdx++) {
			std::string outputName = QNN_TENSOR_GET_NAME(outputs[outputIdx]);
			m_outputName.push_back(outputName);
		}
	}
	return m_outputName;
}

std::vector<std::string> sample_app::QnnSampleApp::getInputDataType(){
	if(m_inputDataType_s.empty()){
		auto graphInfo = (*m_graphsInfo)[0/*graphIdx*/];
		Qnn_Tensor_t* inputs  = graphInfo.m_inputs;
		for (size_t inputIdx = 0; inputIdx < graphInfo.numInputTensors; inputIdx++) {
			Qnn_DataType_t dims_inputDataType = QNN_TENSOR_GET_DATA_TYPE(inputs[inputIdx]);
			m_inputDataType_s.push_back(dataTypeToString(dims_inputDataType));
		}
	}
#ifdef DEBUG_INFERENCE
    printf("[DEBUG]m_inputDataType_s:");
    printDataTypes(m_inputDataType_s);
#endif 
    return m_inputDataType_s;  
}

std::vector<std::vector<size_t>> sample_app::QnnSampleApp::getOutputShapes(){
	if(m_outputShapes.empty()){
		auto graphInfo = (*m_graphsInfo)[0/*graphIdx*/];
		Qnn_Tensor_t* outputs  = graphInfo.m_outputs;
		for (size_t outputIdx = 0; outputIdx < graphInfo.numOutputTensors; outputIdx++) {
			if (QNN_TENSOR_GET_DIMENSIONS(outputs[outputIdx]) == nullptr || QNN_TENSOR_GET_RANK(outputs[outputIdx]) == 0) {
				//printf("[ERROR] Output tensor %zu has nullptr dimensions or rank == 0\n", outputIdx);
				continue;
			}
			std::vector<size_t> dims;
			m_ioTensor.fillDims(dims, QNN_TENSOR_GET_DIMENSIONS(outputs[outputIdx]), QNN_TENSOR_GET_RANK(outputs[outputIdx]));
			m_outputShapes.push_back(dims);
		}
	}
#ifdef DEBUG_INFERENCE
  printf("[DEBUG]m_outputShapes:");
  printArrayOfVector(m_outputShapes);
#endif
  return m_outputShapes;
}

std::vector<std::string> sample_app::QnnSampleApp::getOutputDataType(){
	if(m_outputDataType_s.empty()){
		auto graphInfo = (*m_graphsInfo)[0/*graphIdx*/];
		Qnn_Tensor_t* outputs  = graphInfo.m_outputs;
		for (size_t outputIdx = 0; outputIdx < graphInfo.numOutputTensors; outputIdx++) {
			Qnn_DataType_t dims_outputDataType = QNN_TENSOR_GET_DATA_TYPE(outputs[outputIdx]);
			m_outputDataType_s.push_back(dataTypeToString(dims_outputDataType));
		}
	}
#ifdef DEBUG_INFERENCE
	printf("[DEBUG]m_outputDataType_s:");
	printDataTypes(m_outputDataType_s);
#endif
	return m_outputDataType_s;
}

sample_app::StatusCode sample_app::QnnSampleApp::executeGraphsBuffers(std::vector<uint8_t*>& inputBuffers, 
                                                                      std::vector<uint8_t*>& outputBuffers, std::vector<size_t>& outputSize,
                                                                      std::string perfProfile, size_t graphIndex, size_t share_memory_size) {
  auto returnStatus = StatusCode::SUCCESS;
  
  if (nullptr == m_graphsInfo || nullptr == (*m_graphsInfo)) {
    QNN_ERROR("m_graphsInfo is nullptr");
    return StatusCode::FAILURE;
  }
  if (graphIndex >= m_graphsCount) {
    QNN_ERROR("Invalid graphIndex: %zu, graphsCount: %zu", graphIndex, m_graphsCount);
    return StatusCode::FAILURE;
  }
  if (inputBuffers.empty()) {
    QNN_ERROR("inputBuffers is empty");
    return StatusCode::FAILURE;
  }

  // We push '12345' to 'outputSize' in function 'ModelRun@main.cpp@SvcQNNHelpper.exe'. In this case, share memory will not be freed, we can use the share memory as output buffer directly.
  bool shareMemory = false;
  uint8_t* pShareBuffer = inputBuffers[0];
  if (outputSize.size() == 1 && outputSize[0] == 12345) {
      shareMemory = true;
      outputSize.clear();

      // Find the share buffer entry point, the smalest point address in 'inputBuffers'.
      for (size_t i = 0; i < inputBuffers.size(); i++) {
          if (pShareBuffer > inputBuffers[i])
              pShareBuffer = inputBuffers[i];
      }
  }

  // printf("m_graphsCount = %d\n", m_graphsCount);
  
  size_t graphIdx = graphIndex; // Only run one graph at a time.
  // for (size_t graphIdx = 0; graphIdx < m_graphsCount; graphIdx++) {
    // printf("Starting execution for graphIdx: %d\n", (int)graphIdx);

  QNN_DEBUG("Starting execution for graphIdx: %d", graphIdx);
    //if (graphIdx >= inputBuffers.size()) {
    //  QNN_ERROR("No Inputs available for: %d", graphIdx);
    //  returnStatus = StatusCode::FAILURE;
    //  return returnStatus;
    //}

    // improve performance.

  Qnn_Tensor_t* inputs  = (*m_graphsInfo)[graphIdx].m_inputs ;
  Qnn_Tensor_t* outputs = (*m_graphsInfo)[graphIdx].m_outputs;

  auto graphInfo = (*m_graphsInfo)[graphIdx];

  if (nullptr == inputs || nullptr == outputs) {
    QNN_ERROR("inputs/outputs is nullptr for graphIdx: %zu", graphIdx);
    return StatusCode::FAILURE;
  }

  // When using shared memory, validate share_memory_size is sufficient for the provided input buffers.
  // NOTE: Only validate when share_memory_size > 0. If share_memory_size == 0, treat it as "unknown/unlimited".
  if (shareMemory && share_memory_size > 0) {
    if (inputBuffers.size() != graphInfo.numInputTensors) {
      QNN_ERROR("Incorrect amount of Input Buffers for graphIdx: %zu. Expected: %d, received: %zu", graphIdx, graphInfo.numInputTensors, inputBuffers.size());
      return StatusCode::FAILURE;
    }

    size_t requiredInputBytesTotal = 0;
    for (size_t inputIdx = 0; inputIdx < graphInfo.numInputTensors; inputIdx++) {
      std::vector<size_t> dims;
      if (QNN_TENSOR_GET_DIMENSIONS(inputs[inputIdx]) == nullptr ||
        QNN_TENSOR_GET_RANK(inputs[inputIdx]) == 0) {
        QNN_ERROR("Input tensor %zu has nullptr dimensions or rank == 0", inputIdx);
        return StatusCode::FAILURE;
      }
      m_ioTensor.fillDims(dims, QNN_TENSOR_GET_DIMENSIONS(inputs[inputIdx]), QNN_TENSOR_GET_RANK(inputs[inputIdx]));

      size_t bytesNeeded = 0;
      const Qnn_DataType_t inDtype = QNN_TENSOR_GET_DATA_TYPE(inputs[inputIdx]);
      if (m_inputDataType == InputDataType::FLOAT && inDtype != QNN_DATATYPE_FLOAT_32) {
        // Caller provides float32 input when model expects non-float input (conversion path).
        bytesNeeded = datautil::calculateElementCount(dims) * sizeof(float);
      } else {
        datautil::StatusCode duStatus;
        std::tie(duStatus, bytesNeeded) = datautil::calculateLength(dims, inDtype);
        if (datautil::StatusCode::SUCCESS != duStatus ||
        bytesNeeded == 0) {
          QNN_ERROR("Failed to calculate native input size for inputIdx: %zu", inputIdx);
          return StatusCode::FAILURE;
        }
      }

      // Compute the span end offset (relative to shared memory base) for this input buffer.
      ptrdiff_t delta = inputBuffers[inputIdx] - pShareBuffer;
      if (delta < 0) {
        QNN_ERROR("Invalid shared buffer layout: inputIdx=%zu is below shared base pointer", inputIdx);
        return StatusCode::FAILURE;
      }
      const size_t offsetBytes = static_cast<size_t>(delta);
      if (offsetBytes > (std::numeric_limits<size_t>::max)() - bytesNeeded) {
        QNN_ERROR("share memory size overflow while accumulating required input bytes");
        return StatusCode::FAILURE;
      }
      const size_t endBytes = offsetBytes + bytesNeeded;
      if (requiredInputBytesTotal < endBytes) {
        requiredInputBytesTotal = endBytes;
      }
      if (requiredInputBytesTotal > share_memory_size) {
        QNN_ERROR("share memory size(%zu) is insufficient for inputs. required=%zu bytes, graphIdx=%zu", share_memory_size, requiredInputBytesTotal, graphIdx);
        return StatusCode::FAILURE;
      }
    }

    QNN_WARN("share memory size(%zu) is enough for inputs. required=%zu bytes, graphIdx=%zu",
    share_memory_size, requiredInputBytesTotal, graphIdx);
  }

    // printf("graphName: %s, numInputTensors: %d, numOutputTensors: %d\n", graphInfo.graphName, graphInfo.numInputTensors, graphInfo.numOutputTensors);

    if (!inputBuffers.empty()) {
      //size_t totalCount = inputFileList[0].size();
      //while (!inputFileList[0].empty()) 
      {
          // size_t startIdx = 0;  // (totalCount - inputFileList[0].size());
          if (iotensor::StatusCode::SUCCESS !=
            m_ioTensor.populateInputTensors((uint32_t)graphIdx, inputBuffers, inputs, graphInfo, m_inputDataType)) {
            returnStatus = StatusCode::FAILURE;
          }

#ifdef DEBUG_INFERENCE
          std::vector<size_t> inputSize;
          m_ioTensor.getTensorsSize(&inputs, (*m_graphsInfo)[graphIdx].numInputTensors, (*m_graphsInfo)[graphIdx].inputTensors, inputSize);
          std::string data_name = "input_%d.raw";
          bufferToFile(inputBuffers, inputSize, data_name);
#endif

        if (StatusCode::SUCCESS == returnStatus) {
          QNN_DEBUG("Successfully populated input tensors for graphIdx: %d", graphIdx);
          Qnn_ErrorHandle_t executeStatus = QNN_GRAPH_NO_ERROR;

          if (false == m_runInCpu && "default" != perfProfile && false == boostPerformance(m_perfInfra, perfProfile)) {
            QNN_ERROR("Performance boost failure");
          }

          executeStatus =
              m_qnnFunctionPointers.qnnInterface.graphExecute(graphInfo.graph,
                                                              inputs,
                                                              graphInfo.numInputTensors,
                                                              outputs,
                                                              graphInfo.numOutputTensors,
                                                              m_profileBackendHandle,
                                                              nullptr);

          if (false == m_runInCpu && "default" != perfProfile && false == resetPerformance(m_perfInfra)) {
            QNN_ERROR("Performance reset failure");
          }

          if (ProfilingLevel::OFF != m_profilingLevel) {
            extractBackendProfilingInfo(m_profileBackendHandle);
          }

          if (QNN_GRAPH_NO_ERROR != executeStatus) {
            returnStatus = StatusCode::FAILURE;
          }

          if (StatusCode::SUCCESS == returnStatus) {
            QNN_DEBUG("Successfully executed graphIdx: %d ", graphIdx);

            // Pre-calc each output write size once, then:
            // 1) use it for the single shared-memory size validation
            // 2) use it to advance offset during output packing
            std::vector<size_t> outNativeBytes(graphInfo.numOutputTensors, 0);
            std::vector<size_t> outFloatBytes(graphInfo.numOutputTensors, 0);
            std::vector<size_t> outBytesToWrite(graphInfo.numOutputTensors, 0);
            std::vector<Qnn_DataType_t> outDtypes(graphInfo.numOutputTensors, QNN_DATATYPE_UNDEFINED);
            for (size_t outputIdx = 0; outputIdx < graphInfo.numOutputTensors; outputIdx++) {
              std::vector<size_t> dims;
              m_ioTensor.fillDims(dims, QNN_TENSOR_GET_DIMENSIONS(outputs[outputIdx]), QNN_TENSOR_GET_RANK(outputs[outputIdx]));
              const Qnn_DataType_t outDtype = QNN_TENSOR_GET_DATA_TYPE(outputs[outputIdx]);
              outDtypes[outputIdx] = outDtype;

              datautil::StatusCode duStatus;
              std::tie(duStatus, outNativeBytes[outputIdx]) = datautil::calculateLength(dims, outDtype);
              if (datautil::StatusCode::SUCCESS != duStatus || outNativeBytes[outputIdx] == 0) {
                QNN_ERROR("Failed to calculate native output size for outputIdx: %d", outputIdx);
                return StatusCode::FAILURE;
              }
              outFloatBytes[outputIdx] = datautil::calculateElementCount(dims) * sizeof(float);

              if (outDtype != QNN_DATATYPE_FLOAT_32 && m_outputDataType == OutputDataType::FLOAT_ONLY) {
                outBytesToWrite[outputIdx] = outFloatBytes[outputIdx];
              } else {
                outBytesToWrite[outputIdx] = outNativeBytes[outputIdx];
              }
            }

            // When using shared memory output, validate share_memory_size before writing any output.
            // NOTE: Only validate when share_memory_size > 0. If share_memory_size == 0, treat it as "unknown/unlimited".
            if (shareMemory && share_memory_size > 0) {
              size_t requiredBytesTotal = 0;
              for (size_t outputIdx = 0; outputIdx < graphInfo.numOutputTensors; outputIdx++) {
                if (requiredBytesTotal > (std::numeric_limits<size_t>::max)() - outBytesToWrite[outputIdx]) {
                  QNN_ERROR("share memory size overflow while accumulating required bytes");
                  return StatusCode::FAILURE;
                }
                requiredBytesTotal += outBytesToWrite[outputIdx];
                if (requiredBytesTotal > share_memory_size) {
                  QNN_ERROR("share memory size(%zu) is insufficient. required=%zu bytes, graphIdx=%zu",
                            share_memory_size, requiredBytesTotal, graphIdx);
                  return StatusCode::FAILURE;
                }
              }

              QNN_WARN("share memory size(%zu) is enough for outputs. required=%zu bytes, graphIdx=%zu", share_memory_size, requiredBytesTotal, graphIdx);
            }

            // populate output buffer directly
            size_t offset = 0;
            for (size_t outputIdx = 0; outputIdx < graphInfo.numOutputTensors; outputIdx++) {
                QNN_DEBUG("Writing output for outputIdx: %d", outputIdx);

                const Qnn_DataType_t outDtype = outDtypes[outputIdx];
                const size_t nativeBytes      = outNativeBytes[outputIdx];
                const size_t floatBytes       = outFloatBytes[outputIdx];
                const size_t bytesToWrite     = outBytesToWrite[outputIdx];

                uint8_t* buffer = nullptr;      // what we finally push to outputBuffers
                float*   floatBuffer = nullptr; // used only for FLOAT_ONLY conversion path

                // NOTE:
                // - When shareMemory is enabled, caller provides a shared region and we pack outputs
                //   sequentially into it. We must advance offset by the *actual* bytes we write.
                // - For FLOAT_ONLY we write floatBytes; for NATIVE_ONLY we write nativeBytes.

                if (outDtype == QNN_DATATYPE_FLOAT_32) {
                    QNN_DEBUG("Writing in output->dataType == QNN_DATATYPE_FLOAT_32");
                    // For float output tensor, outputDataType has no effect (same behavior as IOTensor::writeOutputTensors). 
                    if (shareMemory) {
                      buffer = pShareBuffer + offset;
                    } else {
                      buffer = static_cast<uint8_t*>(malloc(nativeBytes));
                      if (!buffer) {
                        QNN_ERROR("Failed to allocate output buffer for outputIdx: %d", outputIdx);
                        return StatusCode::FAILURE;
                      }
                    }
                    memcpy(buffer,
                          reinterpret_cast<uint8_t*>(QNN_TENSOR_GET_CLIENT_BUF(&(outputs[outputIdx])).data),
                          nativeBytes);
                }
                else if (m_outputDataType == OutputDataType::FLOAT_ONLY) {
                    QNN_DEBUG("Writing in output->dataType == OutputDataType::FLOAT_ONLY");
                    if (shareMemory) {
                      floatBuffer = reinterpret_cast<float*>(pShareBuffer + offset);
                    }
                    auto ioReturnStatus = m_ioTensor.convertToFloat(&floatBuffer, &outputs[outputIdx]);
                    if (iotensor::StatusCode::SUCCESS != ioReturnStatus) {
                        QNN_ERROR("failure in convertToFloat");
                        return StatusCode::FAILURE;
                    }
                    buffer = reinterpret_cast<uint8_t*>(floatBuffer);
                }
                else if (m_outputDataType == OutputDataType::NATIVE_ONLY) {
                    QNN_DEBUG("Writing in output->dataType == OutputDataType::NATIVE_ONLY");

                    // Native-only: write as-is (no convertToFloat), equivalent to IOTensor::writeOutputTensor(). 
                    if (shareMemory) {
                      buffer = pShareBuffer + offset;
                    } else {
                      buffer = static_cast<uint8_t*>(malloc(nativeBytes));
                      if (!buffer) {
                        QNN_ERROR("Failed to allocate native output buffer for outputIdx: %d", outputIdx);
                        return StatusCode::FAILURE;
                      }
                    }
                    memcpy(buffer,
                          reinterpret_cast<uint8_t*>(QNN_TENSOR_GET_CLIENT_BUF(&(outputs[outputIdx])).data),
                          nativeBytes);
                }
                else {
                    QNN_ERROR("Can't handle unknown data type: %d", m_outputDataType);
                }

                if (shareMemory) {
                  offset += bytesToWrite;
                }

                if (buffer) {
                    outputBuffers.push_back(buffer);
                    // Report the exact bytes of this output buffer.
                    // - FLOAT_ONLY: floatBytes
                    // - NATIVE_ONLY / float32 tensor: nativeBytes
                    size_t reportedBytes = (m_outputDataType == OutputDataType::FLOAT_ONLY && outDtype != QNN_DATATYPE_FLOAT_32)
                                            ? floatBytes
                                            : nativeBytes;
                    outputSize.push_back(reportedBytes);
                }
            }
            // QNN_ERROR("output buffer size: %d\n", outputBuffers.size());

#ifdef DEBUG_INFERENCE
                      std::string data_name = "output_%d.raw";
                      bufferToFile(outputBuffers, outputSize, data_name);
#endif
          }
        }
        if (StatusCode::SUCCESS != returnStatus) {
          QNN_ERROR("Execution of Graph: %d failed!", graphIdx);
          return returnStatus;
        }
      }
    }
  // }  // End of for.

  return returnStatus;
}

// zw.
sample_app::StatusCode sample_app::QnnSampleApp::freeGraphs() {
  qnn_wrapper_api::freeGraphsInfo(&m_graphsInfo, m_graphsCount);
  m_graphsInfo = nullptr;

  return StatusCode::SUCCESS;
}

sample_app::StatusCode sample_app::QnnSampleApp::initializeLog() {
  // initialize logging in the backend
  if (log::isLogInitialized()) {
    auto logCallback = log::getLogCallback();
    auto logLevel    = log::getLogLevel();
    QNN_INFO("Initializing logging in the backend. Callback: [%p], Log Level: [%d]", logCallback, logLevel);
    if (QNN_SUCCESS !=
        m_qnnFunctionPointers.qnnInterface.logCreate(logCallback, logLevel, &m_logHandle)) {
      QNN_WARN("Unable to initialize logging in the backend.");
    }
  } else {
    QNN_WARN("Logging not available in the backend.");
  }
  return StatusCode::SUCCESS;
}

sample_app::StatusCode sample_app::QnnSampleApp::setLogLevel(QnnLog_Level_t logLevel) {
    if (QNN_SUCCESS != m_qnnFunctionPointers.qnnInterface.logSetLogLevel(m_logHandle, logLevel)) {
        QNN_WARN("Unable to set logging level in the backend.");
    }

    return StatusCode::SUCCESS;
}

// Performance Setting for HTP
sample_app::StatusCode sample_app::QnnSampleApp::initializePerformance() {
    QnnDevice_Infrastructure_t deviceInfra = nullptr;
    if (QNN_SUCCESS != m_qnnFunctionPointers.qnnInterface.deviceGetInfrastructure(&deviceInfra)) {
        QNN_ERROR("Failure in deviceGetInfrastructure()");
        return StatusCode::FAILURE;
    }

    QnnHtpDevice_Infrastructure_t* htpInfra = static_cast<QnnHtpDevice_Infrastructure_t*>(deviceInfra);
    m_perfInfra = htpInfra->perfInfra;
    uint32_t deviceId = 0;
    uint32_t coreId = 0;
    if (QNN_SUCCESS != m_perfInfra.createPowerConfigId(deviceId, coreId, &m_powerConfigId)) {
        QNN_ERROR("Failure in createPowerConfigId()");
        return StatusCode::FAILURE;
    }
    sg_powerConfigIds.insert(m_powerConfigId);
    return StatusCode::SUCCESS;
}

sample_app::StatusCode sample_app::QnnSampleApp::destroyPerformance() {
    if (true == m_runInCpu)
        return StatusCode::SUCCESS;

    if (QNN_SUCCESS != m_perfInfra.destroyPowerConfigId(m_powerConfigId)) {
        QNN_ERROR("Failure in destroyPowerConfigId()");
        return StatusCode::FAILURE;
    }
    sg_powerConfigIds.erase(m_powerConfigId);
    return StatusCode::SUCCESS;
}
