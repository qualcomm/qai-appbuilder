//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#include "ICommandLineManager.hpp"
#include "QnnBackend.h"
#include "QnnContext.h"
#include "QnnGraph.h"
#include "QnnLog.h"
#include "QnnTypeDef.hpp"

#define QNN_ENABLE_API_2x
#define QNN_ENABLE_API_2x_P2

#ifdef QNN_ENABLE_API_2x
#include "QnnProfile.h"
#endif

#ifdef QNN_ENABLE_API_2x_P2
#include "QnnDevice.h"
#endif

const uint32_t g_profilingLevelNotSet = 0;
enum class PerfProfile {
  LOW_BALANCED,
  BALANCED,
  DEFAULT,
  HIGH_PERFORMANCE,
  SUSTAINED_HIGH_PERFORMANCE,
  BURST,
  LOW_POWER_SAVER,
  POWER_SAVER,
  HIGH_POWER_SAVER,
  SYSTEM_SETTINGS,
  NO_USER_INPUT,
  CUSTOM,
  INVALID
};

// This is the interface that enables backend specific extensions in qnn-net-run.
// It is designed as hooks in the timeline of various events in NetRun.
// Backends that intend to implement custom features through qnn-net-run will have
// to implement this interface and add functionality in appropriate methods depending
// on where/when the custom functionality needs to be exercised.
// These functions/hooks will be called through the IBackend interface from within
// qnn-net-run wherever necessary.
class IBackend {
 public:
  virtual ~IBackend() {}

  virtual bool setupLogging(QnnLog_Callback_t callback, QnnLog_Level_t maxLogLevel) = 0;

  virtual bool initialize(void* backendLibHandle) = 0;

  virtual bool setPerfProfile(PerfProfile perfProfile) = 0;

#ifdef QNN_ENABLE_API_2x
  virtual QnnProfile_Level_t getProfilingLevel() = 0;
#endif

  virtual bool loadConfig(std::string configFile) = 0;

  virtual bool loadCommandLineArgs(std::shared_ptr<ICommandLineManager> clManager) = 0;

  virtual bool beforeBackendInitialize(QnnBackend_Config_t*** customConfigs,
                                       uint32_t* configCount) = 0;

  virtual bool afterBackendInitialize() = 0;

  virtual bool beforeContextCreate(QnnContext_Config_t*** customConfigs, uint32_t* configCount) = 0;

  virtual bool afterContextCreate() = 0;

  virtual bool beforeComposeGraphs(GraphConfigInfo_t*** customGraphConfigs,
                                   uint32_t* graphCount) = 0;

  virtual bool afterComposeGraphs() = 0;

  virtual bool beforeGraphFinalize() = 0;

  virtual bool afterGraphFinalize() = 0;

  virtual bool beforeRegisterOpPackages() = 0;

  virtual bool afterRegisterOpPackages() = 0;

  virtual bool beforeExecute(const char* graphName,
                             QnnGraph_Config_t*** customConfigs,
                             uint32_t* configCount) = 0;

  virtual bool afterExecute() = 0;

  virtual bool beforeContextFree() = 0;

  virtual bool afterContextFree() = 0;

  virtual bool beforeBackendTerminate() = 0;

  virtual bool afterBackendTerminate() = 0;

  virtual bool beforeCreateFromBinary(QnnContext_Config_t*** customConfigs,
                                      uint32_t* configCount) = 0;

  virtual bool afterCreateFromBinary() = 0;

#ifdef QNN_ENABLE_API_2x_P2
  virtual bool beforeCreateDevice(QnnDevice_Config_t*** deviceConfigs, uint32_t* configCount) = 0;

  virtual bool afterCreateDevice() = 0;

  virtual bool beforeFreeDevice() = 0;

  virtual bool afterFreeDevice() = 0;

  virtual bool beforeActivateContext(QnnContext_Config_t*** customConfigs,
                                     uint32_t* configCount) = 0;

  virtual bool afterActivateContext() = 0;

  virtual bool beforeDeactivateContext(QnnContext_Config_t*** customConfigs,
                                       uint32_t* configCount) = 0;

  virtual bool afterDeactivateContext() = 0;
#endif
};

// These are the function types that the backend extensions shared library is
// expected to expose. The first function helps NetRun obtain a valid implementation
// of IBackend interface and the second is used to destroy the same interface at the end.
// The function names themselves are expected to be these strings:
//      1. "createBackendInterface"
//      2. "destroyBackendInterface"
// These functions need to be tagged with extern "C" and their symbols need to be exposed.
typedef IBackend* (*CreateBackendInterfaceFnType_t)();
typedef void (*DestroyBackendInterfaceFnType_t)(IBackend*);
