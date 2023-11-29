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
#include<chrono>
#include <unordered_map>
#include <iostream>

#include "BuildId.hpp"
#include "DynamicLoadUtil.hpp"
#include "Logger.hpp"
#include "PAL/DynamicLoading.hpp"
#include "PAL/GetOpt.hpp"
#include "QnnSampleApp.hpp"
#include "QnnSampleAppUtils.hpp"
#include "libQNNHelper.hpp"
#include "Utils/Utils.hpp"

using namespace qnn;
using namespace qnn::log;
using namespace qnn::tools;

static void* sg_backendHandle{nullptr};
static void* sg_modelHandle{nullptr};
std::unordered_map<std::string, std::unique_ptr<sample_app::QnnSampleApp>> sg_model_map;

namespace qnn {
namespace tools {
namespace libqnnhelper {

std::unique_ptr<sample_app::QnnSampleApp> initQnnSampleApp(std::string cachedBinaryPath,
                                                           std::string backEndPath,
                                                           std::string systemLibraryPath,
                                                           bool loadFromCachedBinary) {
  // Just keep blank for below paths.
  std::string modelPath;
  std::string opPackagePaths;
  std::string saveBinaryName;

  iotensor::OutputDataType parsedOutputDataType   = iotensor::OutputDataType::FLOAT_ONLY;
  iotensor::InputDataType parsedInputDataType     = iotensor::InputDataType::FLOAT;
  sample_app::ProfilingLevel parsedProfilingLevel = sample_app::ProfilingLevel::OFF;
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

  std::unique_ptr<sample_app::QnnSampleApp> app(new sample_app::QnnSampleApp(qnnFunctionPointers, "null", opPackagePaths, sg_backendHandle, "null",
                                                                             debug, parsedOutputDataType, parsedInputDataType, parsedProfilingLevel,
                                                                             dumpOutputs, cachedBinaryPath, saveBinaryName));
    return app;
}

}  // namespace libqnnhelper
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
    g_ProcName = proc_name;
}

bool SetLogLevel(int32_t log_level) {
  if (!qnn::log::initializeLogging()) {
    QNN_ERROR("ERROR: Unable to initialize logging!\n");
    return false;
  }

  if (!log::setLogLevel((QnnLog_Level_t) log_level)) {
    QNN_ERROR("Unable to set log level!\n");
    return false;
  }

  g_logEpoch = getEpoch();
  g_logLevel = log_level;

  return true;
}

void QNN_ERR(const char* fmt, ...) {
    if (QNN_LOG_LEVEL_ERROR > getLogLevel()) {
        return;
    }
    va_list argp;
    va_start(argp, fmt);
    QnnLog_Callback_t logCallback = getLogCallback();
    (*logCallback)(fmt, QNN_LOG_LEVEL_ERROR, getTimediff(), argp);
    va_end(argp);
}

void QNN_WAR(const char* fmt, ...) {
    if (QNN_LOG_LEVEL_WARN > getLogLevel()) {
        return;
    }
    va_list argp;
    va_start(argp, fmt);
    QnnLog_Callback_t logCallback = getLogCallback();
    (*logCallback)(fmt, QNN_LOG_LEVEL_WARN, getTimediff(), argp);
    va_end(argp);
}

void QNN_INF(const char* fmt, ...) {
    if (QNN_LOG_LEVEL_INFO > getLogLevel()) {
        return;
    }
    va_list argp;
    va_start(argp, fmt);
    QnnLog_Callback_t logCallback = getLogCallback();
    (*logCallback)(fmt, QNN_LOG_LEVEL_INFO, getTimediff(), argp);
    va_end(argp);
}

void QNN_VEB(const char* fmt, ...) {
    if (QNN_LOG_LEVEL_VERBOSE > getLogLevel()) {
        return;
    }
    va_list argp;
    va_start(argp, fmt);
    QnnLog_Callback_t logCallback = getLogCallback();
    (*logCallback)(fmt, QNN_LOG_LEVEL_DEBUG, getTimediff(), argp);
    va_end(argp);
}

void QNN_DBG(const char* fmt, ...) {
    if (QNN_LOG_LEVEL_DEBUG > getLogLevel()) {
        return;
    }
    va_list argp;
    va_start(argp, fmt);
    QnnLog_Callback_t logCallback = getLogCallback();
    (*logCallback)(fmt, QNN_LOG_LEVEL_DEBUG, getTimediff(), argp);
    va_end(argp);
}

bool CreateShareMemory(std::string share_memory_name, size_t share_memory_size) {
    return CreateShareMem(share_memory_name, share_memory_size);
}

bool DeleteShareMemory(std::string share_memory_name) {
    return DeleteShareMem(share_memory_name);
}

bool ModelInitializeEx(std::string model_name, std::string proc_name, std::string model_path,
                       std::string backend_lib_path, std::string system_lib_path,
                       std::string backend_ext_lib_path, std::string backend_ext_config_path) {
  BOOL result = false;

  QNN_INF("libQNNHelper::ModelInitialize: %s \n", model_name.c_str());

  if(!proc_name.empty()) {
    // If proc_name, create process and save process info & model name to map, load model in new process.
    result = TalkToSvc_Initialize(model_name, proc_name, model_path, backend_lib_path, system_lib_path, backend_ext_lib_path, backend_ext_config_path);
    return result;
  }

  TimerHelper timerHelper;

  bool loadFromCachedBinary{ true };
  std::string cachedBinaryPath = model_path;
  std::string backEndPath = backend_lib_path;
  std::string systemLibraryPath = system_lib_path;
  std::string backEndExtLibraryPath = backend_ext_lib_path;
  std::string backEndExtConfigPath = backend_ext_config_path;

  if (!qnn::log::initializeLogging()) {
    QNN_ERROR("ERROR: Unable to initialize logging!\n");
    return false;
  }

  {
    std::unique_ptr<sample_app::QnnSampleApp> app = libqnnhelper::initQnnSampleApp(cachedBinaryPath, backEndPath, systemLibraryPath, loadFromCachedBinary);

    if (nullptr == app) {
      return false;
    }

    QNN_INFO("libQNNHelper   build version: %s", qnn::tools::getBuildId().c_str());
    QNN_INFO("Backend        build version: %s", app->getBackendBuildId().c_str());

    app->initializeLog();

    if (sample_app::StatusCode::SUCCESS != app->initializeBackendExtensions(BackendExtensionsConfigs(backEndExtLibraryPath, backEndExtConfigPath))) {
        app->reportError("Backend Extensions Initialization failure");
        return false;
    }

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

    if (sample_app::StatusCode::SUCCESS != app->initializePerformance()) {
        app->reportError("Performance initialization failure");
        return false;
    }

    timerHelper.Print("model_initialize");

    sg_model_map.insert(std::make_pair(model_name, std::move(app)));

    return true;
  }

  return false;
}

bool ModelInferenceEx(std::string model_name, std::string proc_name, std::string share_memory_name,
                      std::vector<uint8_t*>& inputBuffers, std::vector<size_t>& inputSize,
                      std::vector<uint8_t*>& outputBuffers, std::vector<size_t>& outputSize) {
    BOOL result = true;

    //QNN_INF("libQNNHelper::ModelInference: %s \n", model_name.c_str());

    if (!proc_name.empty()) {
        // If proc_name, run the model in that process.
        result = TalkToSvc_Inference(model_name, proc_name, share_memory_name, inputBuffers, inputSize, outputBuffers, outputSize);
        return result;
    }

    TimerHelper timerHelper;

    std::unique_ptr<sample_app::QnnSampleApp> app = getQnnSampleApp(model_name);

    if (result && nullptr == app) {
        app->reportError("Inference failure");
        result = false;
    }

    if (result && sample_app::StatusCode::SUCCESS != app->boostPerformance()) {
        app->reportError("Performance boost failure");
        result = false;
    }

    if (result && sample_app::StatusCode::SUCCESS != app->executeGraphsBuffers(inputBuffers, outputBuffers, outputSize)) {
        app->reportError("Graph Execution failure");
        result = false;
    }

    if (result && sample_app::StatusCode::SUCCESS != app->resetPerformance()) {
        app->reportError("Performance reset failure");
        result = false;
    }

    sg_model_map.insert(std::make_pair(model_name, std::move(app)));

    timerHelper.Print("model_inference");

    return result;
}

bool ModelDestroyEx(std::string model_name, std::string proc_name) {
    BOOL result = false;

    QNN_INF("libQNNHelper::ModelDestroy: %s \n", model_name.c_str());

    if (!proc_name.empty()) {
        // If proc_name, desctroy the model in that process.
        result = TalkToSvc_Destroy(model_name, proc_name);
        return result;
    }

    TimerHelper timerHelper;

    std::unique_ptr<sample_app::QnnSampleApp> app = getQnnSampleApp(model_name);
    if (nullptr == app) {
        app->reportError("Can't find the model with model_name: " + model_name);
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
/// Class libQNNHelper implementation.
/////////////////////////////////////////////////////////////////////////////

bool LibQNNHelper::ModelInitialize(std::string model_name, std::string proc_name, std::string model_path,
                                   std::string backend_lib_path, std::string system_lib_path,
                                   std::string backend_ext_lib_path, std::string backend_ext_config_path) {
    if (!proc_name.empty()) {   // Create process and save process info & model name to map, load model in new process.
        return TalkToSvc_Initialize(model_name, proc_name, model_path, backend_lib_path, system_lib_path, backend_ext_lib_path, backend_ext_config_path);
    }

    return false;
}

bool LibQNNHelper::ModelInitialize(std::string model_name, std::string model_path,
                                   std::string backend_lib_path, std::string system_lib_path,
                                   std::string backend_ext_lib_path, std::string backend_ext_config_path) {
    return ModelInitializeEx(model_name, "", model_path, backend_lib_path, system_lib_path, backend_ext_lib_path, backend_ext_config_path);
}

bool LibQNNHelper::ModelInference(std::string model_name, std::string proc_name, std::string share_memory_name,
                                  std::vector<uint8_t*>& inputBuffers, std::vector<size_t>& inputSize,
                                  std::vector<uint8_t*>& outputBuffers, std::vector<size_t>& outputSize) {
    if (!proc_name.empty()) {   // TODO: If proc_name, run the model in that process.
        return TalkToSvc_Inference(model_name, proc_name, share_memory_name, inputBuffers, inputSize, outputBuffers, outputSize);
    }

    return false;
}

bool LibQNNHelper::ModelInference(std::string model_name,
                                  std::vector<uint8_t*>& inputBuffers, 
                                  std::vector<uint8_t*>& outputBuffers, std::vector<size_t>& outputSize){
    std::vector<size_t> inputSize;
    return ModelInferenceEx(model_name, "", "", inputBuffers, inputSize, outputBuffers, outputSize);
}

bool LibQNNHelper::ModelDestroy(std::string model_name, std::string proc_name) {
    if (!proc_name.empty()) {   // TODO: If proc_name, desctroy the model in that process.
        return TalkToSvc_Destroy(model_name, proc_name);
    }

    return false;
}

bool LibQNNHelper::ModelDestroy(std::string model_name) {
    return ModelDestroyEx(model_name, "");
}

bool LibQNNHelper::CreateShareMemory(std::string share_memory_name, size_t share_memory_size) {
    return CreateShareMem(share_memory_name, share_memory_size);
}

bool LibQNNHelper::DeleteShareMemory(std::string share_memory_name) {
    return DeleteShareMem(share_memory_name);
}


/////////////////////////////////////////////////////////////////////////////
/// main function, just for test.
/////////////////////////////////////////////////////////////////////////////
#include <iostream>
#include <fstream>
#define UNET_DATA_PATH_IN       "C:\\Source\\SD_QC\\ControlNet\\controlnet_workspace\\backup\\tmp_unet\\input_%d.raw"
#define UNET_DATA_PATH_OUT      "C:\\Source\\SD_QC\\ControlNet\\controlnet_workspace\\backup\\tmp_unet\\output_%d.raw"
#define UNET_DATA_CNT           16
#define BUFSIZE                 256

int main(int argc, char** argv) {
    BOOL result = false;

    std::string UNET_NAME = "unet";
    std::string PROC_NAME = "unet";
    std::string WORK_PATH = "C:\\Source\\SD_QC\\ControlNet\\controlnet_workspace\\qnn_assets\\";
    std::string qnn_binary_path = WORK_PATH + "QNN_binaries\\";

    std::string unet_memory_name = UNET_NAME;
    std::string model_name = UNET_NAME;
    std::string proc_name = PROC_NAME;
    std::string model_path = WORK_PATH + "controlnet_models\\serialized_binaries\\unet_quantized.serialized.v68.bin";
    std::string backend_lib_path = qnn_binary_path + "QnnHtp.dll";
    std::string system_lib_path = qnn_binary_path + "QnnSystem.dll";
    std::string backend_ext_lib_path = qnn_binary_path + "QnnHtpNetRunExtensions.dll";
    std::string backend_ext_config_path = qnn_binary_path + "htp_backend_ext_config_V68.json";

    SetLogLevel(2);

    QNN_INF("Load data from raw data file to vector Start.\n");
    std::vector<uint8_t*> inputBuffers;
    std::vector<size_t> inputSize;
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;
    char dataPath[BUFSIZE];

    for (int i = 0; i < UNET_DATA_CNT; i++) {
        sprintf_s(dataPath, BUFSIZE, UNET_DATA_PATH_IN, i);
        std::ifstream in(dataPath, std::ifstream::binary);
        if (!in) {
            QNN_ERR("Failed to open input file: %s", dataPath);
        }
        else {
            uint8_t* buffer = nullptr;
            in.seekg(0, in.end);
            const size_t length = in.tellg();
            in.seekg(0, in.beg);
            buffer = (uint8_t*)malloc(length);
            if (!in.read(reinterpret_cast<char*>(buffer), length)) {
                QNN_ERR("Failed to read the of: %s", dataPath);
            }

            inputBuffers.push_back(reinterpret_cast<uint8_t*>(buffer));
            inputSize.push_back(length);
        }
        in.close();
    }
    QNN_INF("Load data from raw data file to vector End.\n");

    Print_MemInfo("Load data from raw data file End.");

    // TODO: test code, load and run model locally.
    {
        QNN_INF("Load and run model locally Start.\n");
        result = ModelInitializeEx(model_name.c_str(), "", model_path, backend_lib_path, system_lib_path, backend_ext_lib_path, backend_ext_config_path);
        Print_MemInfo("ModelInitialize End.");

        for (int i = 0; i < 3; i++) {
            result = ModelInferenceEx(model_name.c_str(), "", "", inputBuffers, inputSize, outputBuffers, outputSize);
            Print_MemInfo("ModelInference End.");
        }

        result = ModelDestroyEx(model_name.c_str(), "");
        QNN_INF("Load and run model locally End.\n");
        Print_MemInfo("Load and run model locally End.");
    }

    if (sg_backendHandle) {
        pal::dynamicloading::dlClose(sg_backendHandle);
        sg_backendHandle = nullptr;
    }
    if (sg_modelHandle) {
        pal::dynamicloading::dlClose(sg_modelHandle);
        sg_modelHandle = nullptr;
    }

    return EXIT_SUCCESS;
}
