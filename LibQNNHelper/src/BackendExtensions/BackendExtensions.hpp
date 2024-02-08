//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#include <string>
//#include <dlfcn.h>

#include "IBackend.hpp"
#include "QnnConfig.hpp"
#include "Log.hpp"

// This is a wrapper class that handles resources/state related to
// backend extensions interface. This is used by QnnNetRun library
// to manage and call into an IBackend interface implementation.
// Functionality present in this class:
//      1. Receives the argument string related to backend_extensions
//         argument from the front end and processes it to open the
//         backend extensions library.
//      2. Locates and stores symbols for creating and destroying the
//         IBackend interface implementation.
//      3. If there is no backend_extensions argument, this class creates
//         the dummy IBackend implementation aka NetRunBackend.
//      4. Gives QnnNetRun access to the implementation itself through
//         interface1() function.
class BackendExtensions final {
 public:
  BackendExtensions(BackendExtensionsConfigs backendExtensionsConfig,
                    void* backendLibHandle,
                    PerfProfile perfProfile,
                    std::shared_ptr<ICommandLineManager> clManager =
                        std::shared_ptr<ICommandLineManager>(nullptr));
  ~BackendExtensions();
  bool initialize();
  IBackend* interface1();

 private:
  bool loadFunctionPointers();
  std::string m_backendExtensionsLibPath;
  std::string m_backendExtensionsConfigPath;
  IBackend* m_backendInterface;
  bool m_isNetRunBackendInterface;
  CreateBackendInterfaceFnType_t m_createBackendInterfaceFn;
  DestroyBackendInterfaceFnType_t m_destroyBackendInterfaceFn;
  void* m_backendLibHandle;
  PerfProfile m_perfProfile;
  std::shared_ptr<ICommandLineManager> m_clManager;
};
