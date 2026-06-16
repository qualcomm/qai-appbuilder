//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#include "QnnInterface.h"
#include "QnnWrapperUtils.hpp"
#include "System/QnnSystemInterface.h"

namespace qnn {
namespace tools {
namespace qnn_app {

// Graph Related Function Handle Types
typedef qnn_wrapper_api::ModelError_t (*ComposeGraphsFnHandleType_t)(
    Qnn_BackendHandle_t,
    QNN_INTERFACE_VER_TYPE,
    Qnn_ContextHandle_t,
    const qnn_wrapper_api::GraphConfigInfo_t **,
    const uint32_t,
    qnn_wrapper_api::GraphInfo_t ***,
    uint32_t *,
    bool,
    QnnLog_Callback_t,
    QnnLog_Level_t);
typedef qnn_wrapper_api::ModelError_t (*FreeGraphInfoFnHandleType_t)(
    qnn_wrapper_api::GraphInfo_t ***, uint32_t);

typedef struct QnnFunctionPointers {
  ComposeGraphsFnHandleType_t composeGraphsFnHandle;
  FreeGraphInfoFnHandleType_t freeGraphInfoFnHandle;
  QNN_INTERFACE_VER_TYPE qnnInterface;
  QNN_SYSTEM_INTERFACE_VER_TYPE qnnSystemInterface;
  QnnInterface_t qnnInterfaceHandle;
  QnnSystemInterface_t qnnSystemInterfaceHandle;
} QnnFunctionPointers;

}  // namespace qnn_app
}  // namespace tools
}  // namespace qnn

namespace qnn {
namespace tools {
namespace dynamicloadutil {
enum class StatusCode {
  SUCCESS,
  FAILURE,
  FAIL_LOAD_BACKEND,
  FAIL_LOAD_MODEL,
  FAIL_SYM_FUNCTION,
  FAIL_GET_INTERFACE_PROVIDERS,
  FAIL_LOAD_SYSTEM_LIB,
};

StatusCode getQnnFunctionPointers(std::string backendPath,
                                  std::string modelPath,
                                  qnn_app::QnnFunctionPointers* qnnFunctionPointers,
                                  void** backendHandle,
                                  bool loadModelLib,
                                  void** modelHandleRtn);
StatusCode getQnnSystemFunctionPointers(std::string systemLibraryPath,
                                        qnn_app::QnnFunctionPointers* qnnFunctionPointers,
                                        void** systemLibraryHandleRtn);  // zw. Save systemHandle for free later.
}  // namespace dynamicloadutil
}  // namespace tools
}  // namespace qnn
