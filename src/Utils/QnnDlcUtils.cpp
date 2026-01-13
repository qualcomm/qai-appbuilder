//==============================================================================
//
//  Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
//  All rights reserved.
//  Confidential and Proprietary - Qualcomm Technologies, Inc.
//
//==============================================================================

#include "QnnDlcUtils.hpp"

#include <cstring>

#include "Logger.hpp"
#include "QnnSampleAppUtils.hpp"

using namespace qnn;
using namespace qnn::tools;

sample_app::dlc_utils::StatusCode sample_app::dlc_utils::createDlcHandle(
    const QnnSystemInterface_t& systemInterface,
    const std::string& dlcPath,
    QnnLog_Callback_t logCallback,
    QnnLog_Level_t logLevel,
    Qnn_LogHandle_t& logHandle,
    QnnSystemDlc_Handle_t& dlcHandle) {
  QNN_DEBUG("Creating DLC handle from file: %s\n", dlcPath.c_str());
  QNN_DEBUG("Initializing logging in the System Library for DLC. Callback: [%p], Log Level: [%d]\n",
           logCallback,
           logLevel);

  // Initialize logging for DLC
  if (QNN_SUCCESS != systemInterface.QNN_SYSTEM_INTERFACE_VER_NAME.systemLogCreate(logCallback, logLevel, &logHandle)) {
    QNN_ERROR("Unable to initialize logging in the system library for DLC.");
    return StatusCode::FAILURE;
  }

  // Create DLC handle from file
  if (QNN_SUCCESS != systemInterface.QNN_SYSTEM_INTERFACE_VER_NAME.systemDlcCreateFromFile(logHandle, dlcPath.c_str(), &dlcHandle)) {
    QNN_ERROR("Failed to create DLC handle from file: %s", dlcPath.c_str());
    systemInterface.QNN_SYSTEM_INTERFACE_VER_NAME.systemLogFree(logHandle);
    logHandle = nullptr;
    return StatusCode::FAILURE;
  }

  QNN_DEBUG("Successfully created DLC handle\n");
  return StatusCode::SUCCESS;
}

void sample_app::dlc_utils::freeDlcResources(const QnnSystemInterface_t& systemInterface,
                                             QnnSystemDlc_Handle_t& dlcHandle,
                                             Qnn_LogHandle_t& logHandle) {
  if (dlcHandle) {
    systemInterface.QNN_SYSTEM_INTERFACE_VER_NAME.systemDlcFree(dlcHandle);
    dlcHandle = nullptr;
    QNN_DEBUG("Freed DLC handle");
  }
  if (logHandle) {
    systemInterface.QNN_SYSTEM_INTERFACE_VER_NAME.systemLogFree(logHandle);
    logHandle = nullptr;
    QNN_DEBUG("Freed DLC log handle");
  }
}

sample_app::dlc_utils::StatusCode sample_app::dlc_utils::composeGraphsFromDlc(
    const QnnSystemInterface_t& systemInterface,
    QnnSystemDlc_Handle_t dlcHandle,
    Qnn_BackendHandle_t backendHandle,
    Qnn_ContextHandle_t context,
    const QnnInterface_t& qnnInterface,
    qnn_wrapper_api::GraphInfo_t**& graphsInfo,
    uint32_t& graphsCount) {
  QNN_DEBUG("Composing graphs from DLC\n");

  QnnSystemDlc_GraphConfigInfo_t** graphConfigsInfo = nullptr;
  uint32_t numGraphConfigs                          = 0;

  QnnSystemContext_GraphInfo_t* systemGraphInfos = nullptr;
  uint32_t numGraphs                             = 0;

  QNN_DEBUG("Calling systemDlcComposeGraphs");

  auto qnnStatus = systemInterface.QNN_SYSTEM_INTERFACE_VER_NAME.systemDlcComposeGraphs(
      dlcHandle,
      (const QnnSystemDlc_GraphConfigInfo_t**)graphConfigsInfo,
      numGraphConfigs,
      backendHandle,
      context,
      qnnInterface,
      QNN_SYSTEM_CONTEXT_GRAPH_INFO_VERSION_1,
      &systemGraphInfos,
      &numGraphs);

  if (QNN_SUCCESS != qnnStatus) {
    QNN_ERROR("Failed to compose graphs from DLC. Error code: %d", qnnStatus);
    return StatusCode::FAILURE;
  }

  QNN_DEBUG("Successfully composed %d graphs from DLC", numGraphs);

  // Use existing utility function to copy graph info
  if (!sample_app::copyGraphsInfo(systemGraphInfos, numGraphs, graphsInfo)) {
    QNN_ERROR("Failed to copy graphs info from system graph info");
    // Clean up system graph infos
    if (systemGraphInfos) {
      for (uint32_t i = 0; i < numGraphs; i++) {
        if (systemGraphInfos[i].version == QNN_SYSTEM_CONTEXT_GRAPH_INFO_VERSION_1) {
          free(const_cast<char*>(systemGraphInfos[i].graphInfoV1.graphName));
          free(systemGraphInfos[i].graphInfoV1.graphInputs);
          free(systemGraphInfos[i].graphInfoV1.graphOutputs);
        }
      }
      free(systemGraphInfos);
    }
    return StatusCode::FAILURE;
  }

  graphsCount = numGraphs;
  QNN_DEBUG("in sample_app::dlc_utils::composeGraphsFromDlc");
  // Retrieve graph handles
  auto retrieveStatus = retrieveGraphHandles(qnnInterface, context, graphsInfo, graphsCount);
  if (StatusCode::SUCCESS != retrieveStatus) {
    QNN_ERROR("Failed to retrieve graph handles");
    // Clean up allocated graph info
    if (graphsInfo) {
      for (uint32_t gIdx = 0; gIdx < graphsCount; gIdx++) {
        if (graphsInfo[gIdx]) {
          if (nullptr != graphsInfo[gIdx]->graphName) {
            free(graphsInfo[gIdx]->graphName);
            graphsInfo[gIdx]->graphName = nullptr;
          }
          qnn_wrapper_api::freeQnnTensors(graphsInfo[gIdx]->inputTensors,
                                          graphsInfo[gIdx]->numInputTensors);
          qnn_wrapper_api::freeQnnTensors(graphsInfo[gIdx]->outputTensors,
                                          graphsInfo[gIdx]->numOutputTensors);
        }
      }
      free(*graphsInfo);
    }
    free(graphsInfo);
    graphsInfo = nullptr;
  }

  // Clean up system graph infos
  if (systemGraphInfos) {
    for (uint32_t i = 0; i < numGraphs; i++) {
      if (systemGraphInfos[i].version == QNN_SYSTEM_CONTEXT_GRAPH_INFO_VERSION_1) {
        free(const_cast<char*>(systemGraphInfos[i].graphInfoV1.graphName));
        free(systemGraphInfos[i].graphInfoV1.graphInputs);
        free(systemGraphInfos[i].graphInfoV1.graphOutputs);
      }
    }
    free(systemGraphInfos);
  }
  QNN_DEBUG("sample_app::dlc_utils::composeGraphsFromDlc end");
  return retrieveStatus;
}

sample_app::dlc_utils::StatusCode sample_app::dlc_utils::retrieveGraphHandles(
    const QnnInterface_t& qnnInterface,
    Qnn_ContextHandle_t context,
    qnn_wrapper_api::GraphInfo_t** graphsInfo,
    uint32_t graphsCount) {
  QNN_DEBUG("Retrieving graph handles for %d graphs", graphsCount);

  for (uint32_t graphIdx = 0; graphIdx < graphsCount; graphIdx++) {
    if (QNN_SUCCESS != qnnInterface.QNN_INTERFACE_VER_NAME.graphRetrieve(
                           context,
                           (*graphsInfo)[graphIdx].graphName,
                           &((*graphsInfo)[graphIdx].graph))) {
      QNN_ERROR("Unable to retrieve graph handle for graph: %s",
                (*graphsInfo)[graphIdx].graphName);
      return StatusCode::FAILURE;
    }
    QNN_DEBUG("Retrieved graph handle for: %s", (*graphsInfo)[graphIdx].graphName);
  }

  return StatusCode::SUCCESS;
}
