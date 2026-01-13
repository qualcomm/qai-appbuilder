//==============================================================================
//
//  Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
//  All rights reserved.
//  Confidential and Proprietary - Qualcomm Technologies, Inc.
//
//==============================================================================
#pragma once

#include <string>

#include "QnnInterface.h"
#include "System/QnnSystemInterface.h"
#include "QnnWrapperUtils.hpp"

namespace qnn {
namespace tools {
namespace sample_app {
namespace dlc_utils {

enum class StatusCode {
  SUCCESS,
  FAILURE,
  FAILURE_SYSTEM_ERROR,
  FAILURE_SYSTEM_COMMUNICATION_ERROR
};

/**
 * @brief Create a DLC handle from a file with logging support
 *
 * @param systemInterface QNN System interface containing DLC functions
 * @param dlcPath Path to the DLC file
 * @param logCallback Logging callback function
 * @param logLevel Logging level
 * @param logHandle Output parameter for the created log handle
 * @param dlcHandle Output parameter for the created DLC handle
 * @return StatusCode indicating success or failure
 */
StatusCode createDlcHandle(const QnnSystemInterface_t& systemInterface,
                          const std::string& dlcPath,
                          QnnLog_Callback_t logCallback,
                          QnnLog_Level_t logLevel,
                          Qnn_LogHandle_t& logHandle,
                          QnnSystemDlc_Handle_t& dlcHandle);

/**
 * @brief Free DLC resources including handles
 *
 * @param systemInterface QNN System interface containing DLC functions
 * @param dlcHandle DLC handle to free (will be set to nullptr)
 * @param logHandle Log handle to free (will be set to nullptr)
 */
void freeDlcResources(const QnnSystemInterface_t& systemInterface,
                     QnnSystemDlc_Handle_t& dlcHandle,
                     Qnn_LogHandle_t& logHandle);

/**
 * @brief Compose graphs from DLC and convert to GraphInfo_t format
 *
 * This function:
 * 1. Calls systemDlcComposeGraphs to compose graphs from DLC
 * 2. Converts QnnSystemContext_GraphInfo_t to qnn_wrapper_api::GraphInfo_t
 * 3. Retrieves graph handles from the context
 * 4. Cleans up temporary system graph info structures
 *
 * @param systemInterface QNN System interface containing DLC functions
 * @param dlcHandle Handle to the DLC
 * @param backendHandle Backend handle
 * @param context Context handle
 * @param qnnInterface QNN interface for graph operations
 * @param graphsInfo Output parameter for graph information array
 * @param graphsCount Output parameter for number of graphs
 * @return StatusCode indicating success or failure
 */
StatusCode composeGraphsFromDlc(const QnnSystemInterface_t& systemInterface,
                               QnnSystemDlc_Handle_t dlcHandle,
                               Qnn_BackendHandle_t backendHandle,
                               Qnn_ContextHandle_t context,
                               const QnnInterface_t& qnnInterface,
                               qnn_wrapper_api::GraphInfo_t**& graphsInfo,
                               uint32_t& graphsCount);

/**
 * @brief Retrieve graph handles from context after composition
 *
 * @param qnnInterface QNN interface for graph operations
 * @param context Context handle
 * @param graphsInfo Array of graph information
 * @param graphsCount Number of graphs
 * @return StatusCode indicating success or failure
 */
StatusCode retrieveGraphHandles(const QnnInterface_t& qnnInterface,
                               Qnn_ContextHandle_t context,
                               qnn_wrapper_api::GraphInfo_t** graphsInfo,
                               uint32_t graphsCount);

}  // namespace dlc_utils
}  // namespace sample_app
}  // namespace tools
}  // namespace qnn
