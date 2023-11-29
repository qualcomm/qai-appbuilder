//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#include <string>

#include "ICommandLineManager.hpp"
#include "IBackend.hpp"

// This is an implementation of IBackend interface within qnn-net-run.
// NetRunBackend provides a dummy implementation of IBackend as a concrete
// implementation is needed in case there is no backend extensions library
// supplied by the user.
// This is built as part of QnnNetRun library and is used in case of no
// user supplied backend extensions implementation.
class NetRunBackend final : public IBackend {
 public:
  NetRunBackend() {}

  virtual ~NetRunBackend() {}

  virtual bool setupLogging(QnnLog_Callback_t callback, QnnLog_Level_t maxLogLevel) override {
    ignore(callback);
    ignore(maxLogLevel);
    return true;
  }

  virtual bool initialize(void* backendLibHandle) override {
    ignore(backendLibHandle);
    return true;
  }

  virtual bool setPerfProfile(PerfProfile perfProfile) override {
    ignore(perfProfile);
    return true;
  }

#ifdef QNN_ENABLE_API_2x
  virtual QnnProfile_Level_t getProfilingLevel() override {
    return g_profilingLevelNotSet;
  }
#endif

  virtual bool loadConfig(std::string configFile) override {
    ignore(configFile);
    return true;
  }

  virtual bool loadCommandLineArgs(
      std::shared_ptr<ICommandLineManager> clManager) override {
    ignore(clManager);
    return true;
  }

  virtual bool beforeBackendInitialize(QnnBackend_Config_t*** customConfigs,
                                       uint32_t* configCount) override {
    ignore(customConfigs);
    ignore(configCount);
    return true;
  }

  virtual bool afterBackendInitialize() override { return true; }

  virtual bool beforeContextCreate(QnnContext_Config_t*** customConfigs,
                                   uint32_t* configCount) override {
    ignore(customConfigs);
    ignore(configCount);
    return true;
  }

  virtual bool afterContextCreate() override { return true; }

  virtual bool beforeComposeGraphs(GraphConfigInfo_t*** customGraphConfigs,
                                   uint32_t* graphCount) override {
    ignore(customGraphConfigs);
    ignore(graphCount);
    return true;
  }

  virtual bool afterComposeGraphs() override { return true; }

  virtual bool beforeGraphFinalize() override { return true; }

  virtual bool afterGraphFinalize() override { return true; }

  virtual bool beforeRegisterOpPackages() override { return true; }

  virtual bool afterRegisterOpPackages() override { return true; }

  virtual bool beforeExecute(const char* graphName,
                             QnnGraph_Config_t*** customConfigs,
                             uint32_t* configCount) override {
    ignore(graphName);
    ignore(customConfigs);
    ignore(configCount);
    return true;
  }

  virtual bool afterExecute() override { return true; }

  virtual bool beforeContextFree() override { return true; }

  virtual bool afterContextFree() override { return true; }

  virtual bool beforeBackendTerminate() override { return true; }

  virtual bool afterBackendTerminate() override { return true; }

  virtual bool beforeCreateFromBinary(QnnContext_Config_t*** customConfigs,
                                      uint32_t* configCount) override {
    ignore(customConfigs);
    ignore(configCount);
    return true;
  }

  virtual bool afterCreateFromBinary() override { return true; }

#ifdef QNN_ENABLE_API_2x_P2
  virtual bool beforeCreateDevice(QnnDevice_Config_t*** deviceConfigs,
                                  uint32_t* configCount) override {
    ignore(deviceConfigs);
    ignore(configCount);
    return true;
  }

  virtual bool afterCreateDevice() override { return true; }

  virtual bool beforeFreeDevice() override { return true; }

  virtual bool afterFreeDevice() override { return true; }


    virtual bool beforeActivateContext(QnnContext_Config_t*** customConfigs,
                                     uint32_t* configCount) override {
    ignore(customConfigs);
    ignore(configCount);
    return true;
  }

  virtual bool afterActivateContext() override { return true; }

  virtual bool beforeDeactivateContext(QnnContext_Config_t*** customConfigs,
                                       uint32_t* configCount) override {
    ignore(customConfigs);
    ignore(configCount);
    return true;
  }

  virtual bool afterDeactivateContext() override { return true; }
#endif

private:
  // Utility function to ignore compiler warnings when a variable
  // is unused. Recommended by Herb Sutter in Sutter's Mill
  // instead of (void)variable.
  template <typename T>
  void ignore(const T &) {}
};
