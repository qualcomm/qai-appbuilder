//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "PAL/DynamicLoading.hpp"

#include "BackendExtensions.hpp"
#include "NetRunBackend.hpp"
#include "../Log/Logger.hpp"

BackendExtensions::BackendExtensions(BackendExtensionsConfigs backendExtensionsConfig,
                                     void* backendLibHandle,
                                     PerfProfile perfProfile,
                                     std::shared_ptr<ICommandLineManager> clManager)
    : m_backendExtensionsLibPath(backendExtensionsConfig.sharedLibraryPath),
      m_backendExtensionsConfigPath(backendExtensionsConfig.configFilePath),
      m_backendInterface(nullptr),
      m_isNetRunBackendInterface(false),
      m_createBackendInterfaceFn(nullptr),
      m_destroyBackendInterfaceFn(nullptr),
      m_backendLibHandle(backendLibHandle),
      m_perfProfile(perfProfile),
      m_clManager(clManager) {
#ifdef QNN_ENABLE_API_2x_P2
  (void)m_perfProfile;
#endif
}

BackendExtensions::~BackendExtensions() {
  if (nullptr != m_backendInterface) {
    if (m_isNetRunBackendInterface) {
      QNN_DEBUG("Deleting NetRun Backend Interface");
      delete m_backendInterface;
    } else {
      if (nullptr != m_destroyBackendInterfaceFn) {
        QNN_DEBUG("Destroying Backend Interface");
        m_destroyBackendInterfaceFn(m_backendInterface);
      }
    }
  }
}

bool BackendExtensions::loadFunctionPointers() {
  void* libHandle = pal::dynamicloading::dlOpen(m_backendExtensionsLibPath.c_str(), 
                                                pal::dynamicloading::DL_NOW | pal::dynamicloading::DL_LOCAL);


  if (nullptr == libHandle) {
    QNN_ERROR("Unable to load backend extensions lib: [%s].",
              m_backendExtensionsLibPath.c_str());
    return false;
  }

  m_createBackendInterfaceFn = (CreateBackendInterfaceFnType_t) pal::dynamicloading::dlSym(libHandle, "createBackendInterface");
  m_destroyBackendInterfaceFn = (DestroyBackendInterfaceFnType_t) pal::dynamicloading::dlSym(libHandle, "destroyBackendInterface");
  
  if (nullptr == m_createBackendInterfaceFn || nullptr == m_destroyBackendInterfaceFn) {
    QNN_ERROR("Unable to find symbols.");
    return false;
  }

  return true;
}

bool BackendExtensions::initialize() {

  if (m_backendExtensionsLibPath.empty() && m_backendExtensionsConfigPath.empty()) {
    QNN_WARN("No BackendExtensions lib provided; initializing NetRunBackend Interface");
    m_isNetRunBackendInterface = true;
    m_backendInterface         = new NetRunBackend();
  } else {
    QNN_DEBUG("Loading supplied backend extensions lib.");
    QNN_DEBUG("Backend extensions lib path: %s", m_backendExtensionsLibPath.c_str());
    if (m_backendExtensionsConfigPath.empty()) {
      QNN_DEBUG("Backend extensions lib specified without a config file.");
    } else {
      QNN_DEBUG("Backend extensions config path: %s", m_backendExtensionsConfigPath.c_str());
    }
    if (!loadFunctionPointers()) {
      QNN_ERROR("Failed to load function pointers.");
      return false;
    }
    if (nullptr != m_createBackendInterfaceFn) {
      m_backendInterface = m_createBackendInterfaceFn();
    }
  }
  if (nullptr == m_backendInterface) {
    QNN_ERROR("Unable to load backend extensions interface.");
    return false;
  }
  if (!(m_backendInterface->setupLogging(qnn::log::getLogCallback(), QNN_LOG_LEVEL_ERROR))) {
    QNN_WARN("Unable to initialize logging in backend extensions.");
  }
  if (!m_backendInterface->initialize(m_backendLibHandle)) {
    QNN_ERROR("Unable to initialize backend extensions interface.");
    return false;
  }
  if (!m_backendInterface->setPerfProfile(m_perfProfile)) {
    QNN_ERROR("Unable to set perf profile in  backend extensions interface.");
    return false;
  }
  if (!m_backendInterface->loadConfig(m_backendExtensionsConfigPath)) {
    QNN_ERROR("Unable to load backend extensions interface config.");
    return false;
  }
  if ((m_clManager != nullptr) && !m_backendInterface->loadCommandLineArgs(m_clManager)) {
    QNN_ERROR("Unable to load backend extensions' command line arguments.");
    return false;
  }

  return true;
}

IBackend* BackendExtensions::interface1() { return m_backendInterface; }
