//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
#pragma once

#include "QnnGraph.h"
#include "QnnTypes.h"
#include <vector>

#define QNN_ENABLE_API_2x_P3

struct BackendExtensionsConfigs {
  std::string sharedLibraryPath;
  std::string configFilePath;
  BackendExtensionsConfigs() : sharedLibraryPath(""), configFilePath("") {}
  BackendExtensionsConfigs(std::string sharedLibraryPath, std::string configFilePath) : 
                    sharedLibraryPath(sharedLibraryPath), configFilePath(configFilePath) {}
};

struct ContextConfigs {
  bool priorityPresent;
  Qnn_Priority_t priority;
  ContextConfigs() : priorityPresent(false), priority(QNN_PRIORITY_DEFAULT) {}
};

struct GraphConfigs {
  std::string graphName;
#ifndef QNN_ENABLE_API_2x_P3
  bool asyncExecutionOrderPresent;
  QnnGraph_AsyncExecutionOrder_t asyncExecutionOrder;
  bool asyncExeQueueDepthPresent;
  QnnGraph_AsyncExecutionQueueDepth_t asyncExeQueueDepth;
#endif  //  QNN_ENABLE_API_2x_P3
  bool priorityPresent;
  Qnn_Priority_t priority;
  GraphConfigs()
      : graphName(),
#ifndef QNN_ENABLE_API_2x_P3
        asyncExecutionOrderPresent(false),
        asyncExecutionOrder(QNN_GRAPH_ASYNC_EXECUTION_ORDER_UNDEFINED),
        asyncExeQueueDepthPresent(false),
        asyncExeQueueDepth(QNN_GRAPH_ASYNC_EXECUTION_QUEUE_DEPTH_UNDEFINED),
#endif  //  QNN_ENABLE_API_2x_P3
        priorityPresent(false),
        priority(QNN_PRIORITY_UNDEFINED) {
  }
};

struct ConfigOptions {
  BackendExtensionsConfigs backendExtensionsConfigs;
  ContextConfigs contextConfigs;
  std::vector<GraphConfigs> graphConfigs;
  ConfigOptions() : backendExtensionsConfigs(), contextConfigs(), graphConfigs() {}
};
