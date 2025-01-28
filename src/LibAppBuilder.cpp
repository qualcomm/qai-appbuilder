//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include <iostream>
#include <memory>
#include <string>
#include <chrono>
#include <unordered_map>
#include <iostream>
#include <stdio.h>
#include <stdlib.h>
#include <fcntl.h>

#include "BuildId.hpp"
#include "DynamicLoadUtil.hpp"
#include "Logger.hpp"
#include "PAL/DynamicLoading.hpp"
#include "PAL/GetOpt.hpp"
#include "QnnSampleApp.hpp"
#include "Lora.hpp"
#include "QnnSampleAppUtils.hpp"
#include "LibAppBuilder.hpp"
#ifdef _WIN32
#include <io.h>
#include "Utils/Utils.hpp"
#endif

using namespace qnn;
using namespace qnn::log;
using namespace qnn::tools;

static void* sg_backendHandle{nullptr};
static void* sg_modelHandle{nullptr};
static QNN_INTERFACE_VER_TYPE sg_qnnInterface;

QnnHtpDevice_Infrastructure_t *gs_htpInfra(nullptr);
static bool sg_perf_global = false;

std::unordered_map<std::string, std::unique_ptr<sample_app::QnnSampleApp>> sg_model_map;
static sample_app::ProfilingLevel sg_parsedProfilingLevel = sample_app::ProfilingLevel::OFF;

namespace qnn {
namespace tools {
namespace libappbuilder {

std::unique_ptr<sample_app::QnnSampleApp> initQnnSampleApp(std::string cachedBinaryPath,
                                                           std::string backEndPath,
                                                           std::string systemLibraryPath,
                                                           bool loadFromCachedBinary,
                                                           const std::vector<LoraAdapter>& lora_adapters) {
  // Just keep blank for below paths.
  std::string modelPath;
  std::string cachedBinaryPath2;
  std::string opPackagePaths;
  std::string saveBinaryName;

  if (loadFromCachedBinary) {  // *.bin
      cachedBinaryPath2 = cachedBinaryPath;
  }
  else {    // *.dll
      modelPath = cachedBinaryPath;
  }

  iotensor::OutputDataType parsedOutputDataType   = iotensor::OutputDataType::FLOAT_ONLY;
  iotensor::InputDataType parsedInputDataType     = iotensor::InputDataType::FLOAT;

  bool dumpOutputs                                = true;
  bool debug                                      = false;
  
  sample_app::QnnFunctionPointers qnnFunctionPointers;
  // Load backend and model .so and validate all the required function symbols are resolved
  auto statusCode = dynamicloadutil::getQnnFunctionPointers(backEndPath,
                                                            modelPath,
                                                            &qnnFunctionPointers,
                                                            &sg_backendHandle,
                                                            !loadFromCachedBinary,
                                                            &sg_modelHandle);
  if (dynamicloadutil::StatusCode::SUCCESS != statusCode) {
    if (dynamicloadutil::StatusCode::FAIL_LOAD_BACKEND == statusCode) {
      sample_app::exitWithMessage(
          "Error initializing QNN Function Pointers: could not load backend: " + backEndPath, EXIT_FAILURE);
    } else if (dynamicloadutil::StatusCode::FAIL_LOAD_MODEL == statusCode) {
      sample_app::exitWithMessage(
          "Error initializing QNN Function Pointers: could not load model: " + modelPath, EXIT_FAILURE);
    } else {
      sample_app::exitWithMessage("Error initializing QNN Function Pointers", EXIT_FAILURE);
    }
  }

  if (loadFromCachedBinary) {
    statusCode = dynamicloadutil::getQnnSystemFunctionPointers(systemLibraryPath, &qnnFunctionPointers);
    if (dynamicloadutil::StatusCode::SUCCESS != statusCode) {
      sample_app::exitWithMessage("Error initializing QNN System Function Pointers", EXIT_FAILURE);
    }
  }

  sg_qnnInterface = qnnFunctionPointers.qnnInterface;
  std::unique_ptr<sample_app::QnnSampleApp> app(new sample_app::QnnSampleApp(qnnFunctionPointers, "null", opPackagePaths, sg_backendHandle, "null",
                                                                             debug, parsedOutputDataType, parsedInputDataType, sg_parsedProfilingLevel,
                                                                             dumpOutputs, cachedBinaryPath2, saveBinaryName, lora_adapters));
    return app;
}

}  // namespace libappbuilder
}  // namespace tools
}  // namespace qnn


std::unique_ptr<sample_app::QnnSampleApp> getQnnSampleApp(std::string model_name) {
  auto it = sg_model_map.find(model_name);
  if (it != sg_model_map.end()) {
    if (it->second) {
      auto app = std::move(it->second);
      sg_model_map.erase(it);
      return app;
    }
  }
  return nullptr;
}

void SetProcInfo(std::string proc_name, uint64_t epoch) {
    setEpoch(epoch);
#ifdef _WIN32
    g_ProcName = proc_name;
#endif
}

bool SetProfilingLevel(int32_t profiling_level) {
    sg_parsedProfilingLevel = (sample_app::ProfilingLevel)profiling_level;
#ifdef _WIN32
    g_profilingLevel = profiling_level;
#endif
    return true;
}

bool SetLogLevel(int32_t log_level, const std::string log_path) {
#ifdef _WIN32
  if(log_path != "" && log_path != "None") {
    if (_access(log_path.c_str(), 0) == 0) {
        std::string STD_OUT = log_path + "\\log_out.txt";
        std::string STD_ERR = log_path + "\\log_err.txt";
        freopen(STD_OUT.c_str(), "w+", stdout);
        freopen(STD_ERR.c_str(), "w+", stderr);
    }
  }
#endif

  if (!qnn::log::initializeLogging()) {
    QNN_ERROR("ERROR: Unable to initialize logging!\n");
    return false;
  }

  if (!log::setLogLevel((QnnLog_Level_t) log_level)) {
    QNN_ERROR("Unable to set log level!\n");
    return false;
  }

#ifdef _WIN32
  g_logEpoch = getEpoch();
  g_logLevel = log_level;
#endif
  return true;
}

bool SetPerfProfileGlobal(const std::string& perf_profile) {
    if (nullptr == sg_backendHandle) {
        QNN_ERR("SetPerfProfileGlobal::initialize one model before set perf profile!\n");
        return false;
    }

    if (nullptr == gs_htpInfra) {
        QnnDevice_Infrastructure_t deviceInfra = nullptr;
        Qnn_ErrorHandle_t devErr = sg_qnnInterface.deviceGetInfrastructure(&deviceInfra);

        if (devErr != QNN_SUCCESS) {
            QNN_ERR("SetPerfProfileGlobal::device error");
            return false;
        }
        gs_htpInfra = static_cast<QnnHtpDevice_Infrastructure_t *>(deviceInfra);
    }

    QnnHtpDevice_PerfInfrastructure_t perfInfra = gs_htpInfra->perfInfra;
    QNN_INF("PERF::SetPerfProfileGlobal");
    sg_perf_global = true;

    return boostPerformance(perfInfra, perf_profile);
}

bool RelPerfProfileGlobal() {
    if (false == sg_perf_global) {
      QNN_ERR("You should set perf profile before you release it!\n");
      return false;
    }

    sg_perf_global = false;
    QnnHtpDevice_PerfInfrastructure_t perfInfra = gs_htpInfra->perfInfra;
    QNN_INF("PERF::RelPerfProfileGlobal");

    return resetPerformance(perfInfra);
}

void QNN_ERR(const char* fmt, ...) {
    QnnLog_Callback_t logCallback = getLogCallback();
    if (!logCallback) {return;}

    if (QNN_LOG_LEVEL_ERROR > getLogLevel()) {
        return;
    }
    va_list argp;
    va_start(argp, fmt);
    (*logCallback)(fmt, QNN_LOG_LEVEL_ERROR, getTimediff(), argp);
    va_end(argp);
}

void QNN_WAR(const char* fmt, ...) {
    QnnLog_Callback_t logCallback = getLogCallback();
    if (!logCallback) {return;}

    if (QNN_LOG_LEVEL_WARN > getLogLevel()) {
        return;
    }
    va_list argp;
    va_start(argp, fmt);
    (*logCallback)(fmt, QNN_LOG_LEVEL_WARN, getTimediff(), argp);
    va_end(argp);
}

void QNN_INF(const char* fmt, ...) {
    QnnLog_Callback_t logCallback = getLogCallback();
    if (!logCallback) {return;}

    if (QNN_LOG_LEVEL_INFO > getLogLevel()) {
        return;
    }

    va_list argp;
    va_start(argp, fmt);
    (*logCallback)(fmt, QNN_LOG_LEVEL_INFO, getTimediff(), argp);
    va_end(argp);
}

void QNN_VEB(const char* fmt, ...) {
    QnnLog_Callback_t logCallback = getLogCallback();
    if (!logCallback) {return;}

    if (QNN_LOG_LEVEL_VERBOSE > getLogLevel()) {
        return;
    }
    va_list argp;
    va_start(argp, fmt);
    (*logCallback)(fmt, QNN_LOG_LEVEL_DEBUG, getTimediff(), argp);
    va_end(argp);
}

void QNN_DBG(const char* fmt, ...) {
    QnnLog_Callback_t logCallback = getLogCallback();
    if (!logCallback) {return;}

    if (QNN_LOG_LEVEL_DEBUG > getLogLevel()) {
        return;
    }
    va_list argp;
    va_start(argp, fmt);
    (*logCallback)(fmt, QNN_LOG_LEVEL_DEBUG, getTimediff(), argp);
    va_end(argp);
}

bool CreateShareMemory(std::string share_memory_name, size_t share_memory_size) {
#ifdef _WIN32
    return CreateShareMem(share_memory_name, share_memory_size);
#else
    return true;
#endif
}

bool DeleteShareMemory(std::string share_memory_name) {
#ifdef _WIN32
    return DeleteShareMem(share_memory_name);
#else
    return true;
#endif
}

bool ModelInitializeEx(const std::string& model_name, const std::string& proc_name, const std::string& model_path,
                       const std::string& backend_lib_path, const std::string& system_lib_path, 
                       const std::vector<LoraAdapter>& lora_adapters) {
  bool result = false;
  QNN_INF("LibAppBuilder::ModelInitialize: %s \n", model_name.c_str());

#ifdef _WIN32
  if(!proc_name.empty()) {
    // If proc_name, create process and save process info & model name to map, load model in new process.
    result = TalkToSvc_Initialize(model_name, proc_name, model_path, backend_lib_path, system_lib_path);
    return result;
  }
#endif

  TimerHelper timerHelper;

  bool loadFromCachedBinary{ true };
  std::string cachedBinaryPath = model_path;
  std::string backEndPath = backend_lib_path;
  std::string systemLibraryPath = system_lib_path;

  std::string suffix_mode_path = cachedBinaryPath.substr(cachedBinaryPath.find_last_of('.') + 1);
  if (suffix_mode_path == "bin") {  // *.bin
      QNN_INFO("cachedBinaryPath: %s", cachedBinaryPath.c_str());
  }
  else {    // *.dll
      loadFromCachedBinary = false;
      QNN_INFO("modelPath: %s", cachedBinaryPath.c_str());
  }
  // TODO: support *.dlc.


  if (!qnn::log::initializeLogging()) {
    QNN_ERROR("ERROR: Unable to initialize logging!\n");
    return false;
  }

  {
    std::unique_ptr<sample_app::QnnSampleApp> app = libappbuilder::initQnnSampleApp(cachedBinaryPath, backEndPath, systemLibraryPath, loadFromCachedBinary, lora_adapters);

    if (nullptr == app) {
      return false;
    }

    QNN_INFO("LibAppBuilder   build version: %s", qnn::tools::getBuildId().c_str());
    QNN_INFO("Backend        build version: %s", app->getBackendBuildId().c_str());

    app->initializeLog();

    if (sample_app::StatusCode::SUCCESS != app->initializeBackend()) {
      app->reportError("Backend Initialization failure");
      return false;
    }

    auto devicePropertySupportStatus = app->isDevicePropertySupported();
    if (sample_app::StatusCode::FAILURE != devicePropertySupportStatus) {
      auto createDeviceStatus = app->createDevice();
      if (sample_app::StatusCode::SUCCESS != createDeviceStatus) {
        app->reportError("Device Creation failure");
        return false;
      }
    }

    if (sample_app::StatusCode::SUCCESS != app->initializeProfiling()) {
      app->reportError("Profiling Initialization failure");
      return false;
    }

    if (sample_app::StatusCode::SUCCESS != app->registerOpPackages()) {
      app->reportError("Register Op Packages failure");
      return false;
    }

    if (!loadFromCachedBinary) {
      if (sample_app::StatusCode::SUCCESS != app->createContext()) {
        app->reportError("Context Creation failure");
        return false;
      }
      if (sample_app::StatusCode::SUCCESS != app->composeGraphs()) {
        app->reportError("Graph Prepare failure");
        return false;
      }
      if (sample_app::StatusCode::SUCCESS != app->finalizeGraphs()) {
        app->reportError("Graph Finalize failure");
        return false;
      }
    } else {
      if (sample_app::StatusCode::SUCCESS != app->createFromBinary()) {
        app->reportError("Create From Binary failure");
        return false;
      }
    }

    // improve performance.
    if (sample_app::StatusCode::SUCCESS != app->setupInputAndOutputTensors()) {
      app->reportError("Setup Input and Output Tensors failure");
      return false;
    }

    if (loadFromCachedBinary) {
        if (sample_app::StatusCode::SUCCESS != app->initializePerformance()) {
            app->reportError("Performance initialization failure");
            return false;
        }
    }

    // apply lora Adapter on graph
    if (app->binaryUpdates() &&
        sample_app::StatusCode::SUCCESS != app->contextApplyBinarySection(QNN_CONTEXT_SECTION_UPDATABLE)) {
        return app->reportError("Binary update/execution failure");
    }

    timerHelper.Print("model_initialize");

    sg_model_map.insert(std::make_pair(model_name, std::move(app)));

    return true;
  }

  return false;
}

bool ModelInferenceEx(std::string model_name, std::string proc_name, std::string share_memory_name,
                      std::vector<uint8_t*>& inputBuffers, std::vector<size_t>& inputSize,
                      std::vector<uint8_t*>& outputBuffers, std::vector<size_t>& outputSize,
                      std::string& perfProfile) {
    bool result = true;

    //QNN_INF("LibAppBuilder::ModelInference: %s \n", model_name.c_str());

#ifdef _WIN32
    if (!proc_name.empty()) {
        // If proc_name, run the model in that process.
        result = TalkToSvc_Inference(model_name, proc_name, share_memory_name, inputBuffers, inputSize, outputBuffers, outputSize, perfProfile);
        return result;
    }
#endif

    TimerHelper timerHelper;

    std::unique_ptr<sample_app::QnnSampleApp> app = getQnnSampleApp(model_name);

    if (result && nullptr == app) {
        app->reportError("Inference failure");
        result = false;
    }

    if (result && sample_app::StatusCode::SUCCESS != app->executeGraphsBuffers(inputBuffers, outputBuffers, outputSize, perfProfile)) {
        app->reportError("Graph Execution failure");
        result = false;
    }

    sg_model_map.insert(std::make_pair(model_name, std::move(app)));

    timerHelper.Print("model_inference");

    return result;
}

bool ModelDestroyEx(std::string model_name, std::string proc_name) {
    bool result = false;

    QNN_INF("LibAppBuilder::ModelDestroy: %s \n", model_name.c_str());

#ifdef _WIN32
    if (!proc_name.empty()) {
        // If proc_name, desctroy the model in that process.
        result = TalkToSvc_Destroy(model_name, proc_name);
        return result;
    }
#endif

    TimerHelper timerHelper;

    std::unique_ptr<sample_app::QnnSampleApp> app = getQnnSampleApp(model_name);
    if (nullptr == app) {
        app->reportError("Can't find the model with model_name: " + model_name);
        return false;
    }

    // improve performance.
    if (sample_app::StatusCode::SUCCESS != app->tearDownInputAndOutputTensors()) {
        app->reportError("Input and Output Tensors destroy failure");
        return false;
    }

    if (sample_app::StatusCode::SUCCESS != app->destroyPerformance()) {
        app->reportError("Performance destroy failure");
        return false;
    }

    if (sample_app::StatusCode::SUCCESS != app->freeGraphs()) {
        app->reportError("Free graphs failure");
        return false;
    }

    if (sample_app::StatusCode::SUCCESS != app->freeContext()) {
        app->reportError("Context Free failure");
        return false;
    }

    auto devicePropertySupportStatus = app->isDevicePropertySupported();
    if (sample_app::StatusCode::FAILURE != devicePropertySupportStatus) {
        auto freeDeviceStatus = app->freeDevice();
        if (sample_app::StatusCode::SUCCESS != freeDeviceStatus) {
            app->reportError("Device Free failure");
            return false;
        }
    }

    timerHelper.Print("model_destroy");

    return true;
}


/////////////////////////////////////////////////////////////////////////////
/// Class LibAppBuilder implementation.
/////////////////////////////////////////////////////////////////////////////

bool LibAppBuilder::ModelInitialize(const std::string& model_name, const std::string& proc_name, const std::string& model_path,
                                         const std::string& backend_lib_path, const std::string& system_lib_path) {
#ifdef _WIN32
    if (!proc_name.empty()) {   // Create process and save process info & model name to map, load model in new process.
        return TalkToSvc_Initialize(model_name, proc_name, model_path, backend_lib_path, system_lib_path);
    }
#endif
    return false;
}

bool LibAppBuilder::ModelInitialize(const std::string& model_name, const std::string& model_path,
                                         const std::string& backend_lib_path, const std::string& system_lib_path) {
    std::vector<LoraAdapter> Adapters = std::vector<LoraAdapter>();
    return ModelInitializeEx(model_name, "", model_path, backend_lib_path, system_lib_path, Adapters);   
}

bool LibAppBuilder::ModelInitialize(const std::string& model_name, const std::string& model_path,
                                         const std::string& backend_lib_path, const std::string& system_lib_path,const std::vector<LoraAdapter>& lora_adapters) {
    return ModelInitializeEx(model_name, "", model_path, backend_lib_path, system_lib_path, lora_adapters);
}

bool LibAppBuilder::ModelInference(std::string model_name, std::string proc_name, std::string share_memory_name,
                                        std::vector<uint8_t*>& inputBuffers, std::vector<size_t>& inputSize,
                                        std::vector<uint8_t*>& outputBuffers, std::vector<size_t>& outputSize,
                                        std::string& perfProfile) {
#ifdef _WIN32
    if (!proc_name.empty()) {   // If proc_name, run the model in that process.
        return TalkToSvc_Inference(model_name, proc_name, share_memory_name, inputBuffers, inputSize, outputBuffers, outputSize, perfProfile);
    }
#endif
    return false;
}

bool LibAppBuilder::ModelInference(std::string model_name, std::vector<uint8_t*>& inputBuffers, 
                                        std::vector<uint8_t*>& outputBuffers, std::vector<size_t>& outputSize,
                                        std::string& perfProfile){
    std::vector<size_t> inputSize;
    return ModelInferenceEx(model_name, "", "", inputBuffers, inputSize, outputBuffers, outputSize, perfProfile);
}

bool LibAppBuilder::ModelDestroy(std::string model_name, std::string proc_name) {
#ifdef _WIN32
    if (!proc_name.empty()) {   // If proc_name, desctroy the model in that process.
        return TalkToSvc_Destroy(model_name, proc_name);
    }
#endif
    return false;
}

bool LibAppBuilder::ModelDestroy(std::string model_name) {
    return ModelDestroyEx(model_name, "");
}

bool LibAppBuilder::CreateShareMemory(std::string share_memory_name, size_t share_memory_size) {
#ifdef _WIN32
    return CreateShareMem(share_memory_name, share_memory_size);
#else
    return true;
#endif 
}

bool LibAppBuilder::DeleteShareMemory(std::string share_memory_name) {
#ifdef _WIN32
    return DeleteShareMem(share_memory_name);
#else
        return true;
#endif

}

int main(int argc, char** argv) {

    return EXIT_SUCCESS;
}
