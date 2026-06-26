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
#include <mutex>
#include <iostream>
#include <stdio.h>
#include <stdlib.h>
#include <fcntl.h>
#include <algorithm>
#include <vector>
#include <fstream>

#include "BuildId.hpp"
#include "DynamicLoadUtil.hpp"
#include "Logger.hpp"
#include "LogUtils.hpp"
#include "PAL/DynamicLoading.hpp"
#include "PAL/GetOpt.hpp"
#include "QnnInferenceEngine.hpp"
#include "Lora.hpp"
#include "QnnAppUtils.hpp"
#include "LibAppBuilder.hpp"
#ifdef _WIN32
#include <io.h>
#endif
#include "Utils/Utils.hpp"

#if !defined(__ANDROID__) && !defined(__linux__)
  #include <execution>
#endif

using namespace qnn;
using namespace qnn::log;
using namespace qnn::tools;

static void* sg_backendHandle{nullptr};
static void* sg_modelHandle{nullptr};
static void* sg_systemLibraryHandle{nullptr};

static QNN_INTERFACE_VER_TYPE sg_qnnInterface;

QnnHtpDevice_Infrastructure_t *gs_htpInfra(nullptr);
static bool gs_isGpu = false;
static bool gs_isCpu = false;
static bool sg_perf_global = false;

std::unordered_map<std::string, std::unique_ptr<qnn_app::QnnInferenceEngine>> sg_model_map;
// Guards sg_model_map only. The map is mutated (erase on take, insert on
// return) by every ModelInference/ModelInitialize call. When two pipeline
// threads run different models concurrently (e.g. model a on HTP0 + model 1 on HTP1),
// their erase/insert on this shared map race and corrupt its bucket list.
// This mutex serializes ONLY the map find/erase/insert; executeGraphsBuffers
// (the actual HTP inference) runs OUTSIDE the lock, so parallelism is kept.
static std::mutex sg_model_map_mutex;
static qnn_app::ProfilingLevel sg_parsedProfilingLevel = qnn_app::ProfilingLevel::OFF;

namespace qnn {
namespace tools {
namespace libappbuilder {

std::string getFileNameFromPath(const std::string& path) {
    if (path.empty()) return {};
    size_t pos = path.find_last_of("/\\");
    if (pos == std::string::npos || pos == path.size() - 1) {
        return {}; 
    }
    return path.substr(pos + 1);
}

#if !defined(__ANDROID__) && !defined(__linux__)
void warmup_parallel_stl()
{
    static std::once_flag once;
    std::call_once(once, []{
        constexpr size_t N = 1 << 18;
        static std::vector<int> dummy(N, 0);
        std::for_each(std::execution::par, dummy.begin(), dummy.end(),
                      [](int& x){ x += 1; });
    });
    QNN_WAR("warmup_parallel_stl");
}
#endif

std::unique_ptr<qnn_app::QnnInferenceEngine> initQnnInferenceEngine(std::string cachedBinaryPath, std::string backEndPath, std::string systemLibraryPath,
                                                           bool loadFromCachedBinary, std::vector<LoraAdapter>& lora_adapters,
                                                           const std::string& input_data_type, const std::string& output_data_type, qnn_app::MultiCoreDeviceConfig_t multiCoreDeviceConfig) {
  // Just keep blank for below paths.
  std::string modelPath;
  std::string cachedBinaryPath2;
  std::string opPackagePaths;
  std::string saveBinaryName;
  if (!cachedBinaryPath.empty()){
    saveBinaryName = getFileNameFromPath(cachedBinaryPath);
    QNN_DEBUG("initQnnInferenceEngine saveBinaryName=%s\n", saveBinaryName.c_str());
  }

  if (loadFromCachedBinary) {  // *.bin and *.dlc
      cachedBinaryPath2 = cachedBinaryPath;
  }
  else {    // *.dll
      modelPath = cachedBinaryPath;
  }

  QNN_WARN("input_data_type: %s, output_data_type: %s\n", input_data_type.c_str(), output_data_type.c_str());

  iotensor::InputDataType parsedInputDataType     = iotensor::parseInputDataType(input_data_type);
  iotensor::OutputDataType parsedOutputDataType   = iotensor::parseOutputDataType(output_data_type);

  bool dumpOutputs                                = true;
  bool debug                                      = false;
  
  qnn_app::QnnFunctionPointers qnnFunctionPointers;
  // Load backend and model .so and validate all the required function symbols are resolved
  auto statusCode = dynamicloadutil::getQnnFunctionPointers(backEndPath,
                                                            modelPath,
                                                            &qnnFunctionPointers,
                                                            &sg_backendHandle,
                                                            !loadFromCachedBinary,
                                                            &sg_modelHandle);
  if (dynamicloadutil::StatusCode::SUCCESS != statusCode) {
    if (dynamicloadutil::StatusCode::FAIL_LOAD_BACKEND == statusCode) {
      qnn_app::exitWithMessage(
          "Error initializing QNN Function Pointers: could not load backend: " + backEndPath, EXIT_FAILURE);
    } else if (dynamicloadutil::StatusCode::FAIL_LOAD_MODEL == statusCode) {
      qnn_app::exitWithMessage(
          "Error initializing QNN Function Pointers: could not load model: " + modelPath, EXIT_FAILURE);
    } else {
      qnn_app::exitWithMessage("Error initializing QNN Function Pointers", EXIT_FAILURE);
    }
  }

  if (loadFromCachedBinary) {
    statusCode = dynamicloadutil::getQnnSystemFunctionPointers(systemLibraryPath, &qnnFunctionPointers, &sg_systemLibraryHandle);
    if (dynamicloadutil::StatusCode::SUCCESS != statusCode) {
      qnn_app::exitWithMessage("Error initializing QNN System Function Pointers", EXIT_FAILURE);
    }
  }

#if !defined(__ANDROID__) && !defined(__linux__)
  if ((input_data_type == "float") || (output_data_type == "float")) // We need 'std::transform' only for �float� mode. It need data conversation.
      warmup_parallel_stl();
#endif

  sg_qnnInterface = qnnFunctionPointers.qnnInterface;
  std::unique_ptr<qnn_app::QnnInferenceEngine> app(new qnn_app::QnnInferenceEngine(qnnFunctionPointers, "null", opPackagePaths, sg_backendHandle, "null",
                                                                             debug, parsedOutputDataType, parsedInputDataType, sg_parsedProfilingLevel,
                                                                             dumpOutputs, cachedBinaryPath2, saveBinaryName, lora_adapters, cachedBinaryPath2, multiCoreDeviceConfig));
    return app;
}

}  // namespace libappbuilder
}  // namespace tools
}  // namespace qnn


std::unique_ptr<qnn_app::QnnInferenceEngine> getQnnInferenceEngine(std::string model_name) {
  std::lock_guard<std::mutex> lk(sg_model_map_mutex);
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

// Symmetric counterpart to getQnnInferenceEngine: re-insert the app under the same
// lock. Replaces the bare `sg_model_map.insert(...)` calls scattered across
// ModelInference/ModelInitialize so every map mutation is serialized.
void putQnnApp(std::string model_name,
                     std::unique_ptr<qnn_app::QnnInferenceEngine> app) {
  std::lock_guard<std::mutex> lk(sg_model_map_mutex);
  sg_model_map.insert(std::make_pair(std::move(model_name), std::move(app)));
}
void SetProcInfo(std::string proc_name, uint64_t epoch) {
    setEpoch(epoch);
    g_ProcName = proc_name;
}

bool SetProfilingLevel(int32_t profiling_level) {
    sg_parsedProfilingLevel = (qnn_app::ProfilingLevel)profiling_level;
    g_profilingLevel = profiling_level;
    return true;
}

bool SetLogLevel(int32_t log_level, const std::string log_path) {
#ifdef _WIN32
  if(log_path != "" && log_path != "None") {
    if (_access(log_path.c_str(), 0) == 0) {
        std::string STD_OUT = log_path + "\\log_out.txt";
        std::string STD_ERR = log_path + "\\log_err.txt";
        if (freopen(STD_OUT.c_str(), "w+", stdout) == nullptr) {
            QNN_WARN("Failed to redirect stdout to %s", STD_OUT.c_str());
        }
        if (freopen(STD_ERR.c_str(), "w+", stderr) == nullptr) {
            QNN_WARN("Failed to redirect stderr to %s", STD_ERR.c_str());
        }
    }
  }
#endif

  if (!qnn::log::initializeLogging()) {
    QNN_ERROR("ERROR: Unable to initialize logging!\n");
    return false;
  }

#ifdef __ANDROID__
  // Set log file path for Android from parameter
  if(log_path != "" && log_path != "None") {
    qnn::log::utils::setLogFilePath(log_path);
  }
#endif

  if (!log::setLogLevel((QnnLog_Level_t) log_level)) {
    QNN_ERROR("Unable to set log level!\n");
    return false;
  }

  g_logEpoch = getEpoch();
  g_logLevel = log_level;
  return true;
}

bool SetPerfProfileGlobal(const std::string& perf_profile) {
    // In cross-process mode the model lives in the Svc child process, so the
    // perf profile must be applied there. Forward to all Svc processes; if any
    // exist, that is authoritative and we return its result. With no Svc process
    // (pure in-process mode) fall through to apply it locally.
    if (!sg_proc_info_map.empty()) {
        return TalkToSvc_SetPerfProfileGlobal(perf_profile);
    }

    if (nullptr == sg_backendHandle) {
        QNN_ERR("SetPerfProfileGlobal::initialize one model before set perf profile!\n");
        return false;
    }

    if (gs_isGpu || gs_isCpu) {
        QNN_DEBUG("Skipping HTP performance profile for GPU backend");
        return true;
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
    // Mirror SetPerfProfileGlobal: forward to Svc processes in cross-process mode.
    if (!sg_proc_info_map.empty()) {
        return TalkToSvc_RelPerfProfileGlobal();
    }

    if (gs_isGpu) {
        return true;
    }

    if (false == sg_perf_global) {
      QNN_ERR("You should set perf profile before you release it!\n");
      return false;
    }

    // issue#109/#4: never dereference a null HTP infrastructure handle. This
    // can happen if the backend/context was released between Set and Rel.
    if (nullptr == gs_htpInfra) {
      QNN_ERR("RelPerfProfileGlobal::HTP infrastructure is not available (context released?)\n");
      sg_perf_global = false;
      return false;
    }

    sg_perf_global = false;
    QnnHtpDevice_PerfInfrastructure_t perfInfra = gs_htpInfra->perfInfra;
    QNN_INF("PERF::RelPerfProfileGlobal");

    return resetPerformance(perfInfra);
}

void QNN_ERR(const char* fmt, ...) {
    if (QNN_LOG_LEVEL_ERROR > getLogLevel()) {
        return;
    }
    
    va_list argp;
    va_start(argp, fmt);
    
    QnnLog_Callback_t logCallback = getLogCallback();
    if (logCallback) {
        (*logCallback)(fmt, QNN_LOG_LEVEL_ERROR, getTimediff(), argp);
    }
    
#ifdef __ANDROID__
    va_list argp_copy;
    va_copy(argp_copy, argp);
    qnn::log::utils::logFileCallback(fmt, QNN_LOG_LEVEL_ERROR, getTimediff(), argp_copy);
    va_end(argp_copy);
#endif
    
    va_end(argp);
}

void QNN_WAR(const char* fmt, ...) {
    if (QNN_LOG_LEVEL_WARN > getLogLevel()) {
        return;
    }
    
    va_list argp;
    va_start(argp, fmt);
    
    QnnLog_Callback_t logCallback = getLogCallback();
    if (logCallback) {
        (*logCallback)(fmt, QNN_LOG_LEVEL_WARN, getTimediff(), argp);
    }
    
#ifdef __ANDROID__
    va_list argp_copy;
    va_copy(argp_copy, argp);
    qnn::log::utils::logFileCallback(fmt, QNN_LOG_LEVEL_WARN, getTimediff(), argp_copy);
    va_end(argp_copy);
#endif
    
    va_end(argp);
}

void QNN_INF(const char* fmt, ...) {
    if (QNN_LOG_LEVEL_INFO > getLogLevel()) {
        return;
    }

    va_list argp;
    va_start(argp, fmt);
    
    QnnLog_Callback_t logCallback = getLogCallback();
    if (logCallback) {
        (*logCallback)(fmt, QNN_LOG_LEVEL_INFO, getTimediff(), argp);
    }
    
#ifdef __ANDROID__
    // On Android, also write directly to file log
    va_list argp_copy;
    va_copy(argp_copy, argp);
    qnn::log::utils::logFileCallback(fmt, QNN_LOG_LEVEL_INFO, getTimediff(), argp_copy);
    va_end(argp_copy);
#endif
    
    va_end(argp);
}

void QNN_VEB(const char* fmt, ...) {
    if (QNN_LOG_LEVEL_VERBOSE > getLogLevel()) {
        return;
    }
    
    va_list argp;
    va_start(argp, fmt);
    
    QnnLog_Callback_t logCallback = getLogCallback();
    if (logCallback) {
        (*logCallback)(fmt, QNN_LOG_LEVEL_DEBUG, getTimediff(), argp);
    }
    
#ifdef __ANDROID__
    va_list argp_copy;
    va_copy(argp_copy, argp);
    qnn::log::utils::logFileCallback(fmt, QNN_LOG_LEVEL_VERBOSE, getTimediff(), argp_copy);
    va_end(argp_copy);
#endif
    
    va_end(argp);
}

void QNN_DBG(const char* fmt, ...) {
    if (QNN_LOG_LEVEL_DEBUG > getLogLevel()) {
        return;
    }
    
    va_list argp;
    va_start(argp, fmt);
    
    QnnLog_Callback_t logCallback = getLogCallback();
    if (logCallback) {
        (*logCallback)(fmt, QNN_LOG_LEVEL_DEBUG, getTimediff(), argp);
    }
    
#ifdef __ANDROID__
    va_list argp_copy;
    va_copy(argp_copy, argp);
    qnn::log::utils::logFileCallback(fmt, QNN_LOG_LEVEL_DEBUG, getTimediff(), argp_copy);
    va_end(argp_copy);
#endif
    
    va_end(argp);
}

bool CreateShareMemory(std::string share_memory_name, size_t share_memory_size) {
    return CreateShareMem(share_memory_name, share_memory_size);
}

bool DeleteShareMemory(std::string share_memory_name) {
    return DeleteShareMem(share_memory_name);
}

bool fileExists(const std::string& path) { 
    std::ifstream f(path.c_str()); 
    return f.good(); 
}
std::string stripWhitespace(std::string &str) {
  const std::string whitespace{" \t\n\v\f\r"};
  if (!str.empty()) {
    str.erase(str.begin(), (str.begin() + str.find_first_not_of(whitespace)));
  }
  if (!str.empty() && std::isspace(str.back())) {
    str.erase(str.find_last_not_of(whitespace) + 1);
  }
  return str;
}

void split(std::vector<std::string> &splitString,
                       const std::string &tokenizedString,
                       const char separator) {
  splitString.clear();
  std::istringstream tokenizedStringStream(tokenizedString);
  while (!tokenizedStringStream.eof()) {
    std::string value;
    getline(tokenizedStringStream, value, separator);
    if (!value.empty()) {
      splitString.push_back(value);
    }
  }
}
bool ModelInitializeEx(const std::string& model_name, const std::string& proc_name, const std::string& model_path,
                       const std::string& backend_lib_path, const std::string& system_lib_path, 
                       std::vector<LoraAdapter>& lora_adapters,
                       bool async, const std::string& input_data_type, const std::string& output_data_type, uint32_t deviceID=0, std::string coreIdsStr="") {
  QNN_INFO("LibAppBuilder::ModelInitialize: %s \n", model_name.c_str());

  bool result = false;

  if(!proc_name.empty()) {
    // If proc_name, create process and save process info & model name to map, load model in new process.
    result = TalkToSvc_Initialize(model_name, proc_name, model_path, backend_lib_path, system_lib_path, async, input_data_type, output_data_type);
    return result;
  }

  TimerHelper timerHelper;

  bool loadFromCachedBinary{ true };
  std::string cachedBinaryPath = model_path;
  std::string backEndPath = backend_lib_path;
  std::string systemLibraryPath = system_lib_path;

  // Determine the target backend up front. The cached *.dlc.bin context binary
  // is an HTP-specific serialized context and can only be de-serialized by the
  // HTP backend. Using it with the CPU/GPU backend triggers
  // "Context de-serialization failed" and a subsequent crash, so the cache must
  // only be consumed when running on HTP.
  bool isGpu = backEndPath.find("Gpu") != std::string::npos || backEndPath.find("gpu") != std::string::npos;
  bool isCpu = backEndPath.find("Cpu") != std::string::npos || backEndPath.find("cpu") != std::string::npos;

  std::string suffix_mode_path = cachedBinaryPath.substr(cachedBinaryPath.find_last_of('.') + 1);
  if (suffix_mode_path == "bin") {  // *.bin
      QNN_INFO("cachedBinaryPath: %s", cachedBinaryPath.c_str());
  } else if (suffix_mode_path == "dlc"){
      std::string dlcBinPath = cachedBinaryPath + ".bin";
      if (!isCpu && !isGpu && fileExists(dlcBinPath)) {
          // Only HTP can load the cached context binary.
          cachedBinaryPath = dlcBinPath; 
          suffix_mode_path = "bin";
          QNN_INFO("Found dlc.bin, updated cachedBinaryPath: %s\n", cachedBinaryPath.c_str()); 
      } else if ((isCpu || isGpu) && fileExists(dlcBinPath)) {
          QNN_INFO("Ignoring HTP cache %s for CPU/GPU backend; loading .dlc directly.\n", dlcBinPath.c_str());
      }
  } else {    // *.dll
      loadFromCachedBinary = false;
      QNN_INFO("modelPath: %s", cachedBinaryPath.c_str());
  }

    QNN_INFO("debug deviceID=%d\n", deviceID);
    QNN_INFO("debug coreIdsStr=%s\n", coreIdsStr.c_str());
    if(deviceID > 3){
        QNN_ERROR("Invalid argument passed to device_id: %d. Valid range is 0 for NSP; 1,2,3 for HPASS\n", deviceID);
        return false;
    }   
    qnn_app::MultiCoreDeviceConfig_t multiCoreDevCfg_global ={}; 	
    multiCoreDevCfg_global.deviceId = deviceID;

    std::vector<std::string> coreIdVec = {};
    coreIdsStr = stripWhitespace(coreIdsStr);  // strip any whitespace chars
    split(coreIdVec, coreIdsStr, ','); // use comma delimiter to split codeIds string
    if (coreIdVec.size() > 4) {       // no more than 4 cores
        QNN_ERROR("Invalid number of arguments passed to core_ids: %d. Valid: 0,1,2,3\n", coreIdVec.size());
        return false;
    }

    uint32_t coreID = 0;
    for (size_t c_idx = 0; c_idx < coreIdVec.size(); c_idx++) {
        std::stringstream ss(coreIdVec[c_idx]);
        ss >> coreID;      // to int value
        if (coreID > 3) {  // core_id must be 0~3
            QNN_ERROR("Invalid coreID value passed to core_ids: %d. Valid: 0,1,2,3\n", coreID);
            return false;
        }
        multiCoreDevCfg_global.coreIdVec.push_back(coreID);
    }
  QNN_INFO("[DEBUG]in LibAppBuilder, ModelInitializeEx: isGpu=%d, isCpu=%d, backEndPath=%s\n", (int)isGpu, (int)isCpu, backEndPath.c_str());
  if (!qnn::log::initializeLogging()) {
    QNN_ERROR("ERROR: Unable to initialize logging!\n");
    return false;
  }

  {
    std::unique_ptr<qnn_app::QnnInferenceEngine> app = libappbuilder::initQnnInferenceEngine(cachedBinaryPath, backEndPath, systemLibraryPath, loadFromCachedBinary, lora_adapters, input_data_type, output_data_type, multiCoreDevCfg_global);

    if (nullptr == app) {
      return false;
    }

    QNN_INFO("LibAppBuilder   build version: %s", qnn::tools::getBuildId().c_str());
    QNN_INFO("Backend        build version: %s", app->getBackendBuildId().c_str());

    app->initializeLog();
    app->setIsGpu(isGpu);
    app->setIsCpu(isCpu);

    if (qnn_app::StatusCode::SUCCESS != app->initializeBackend()) {
      app->reportError("Backend Initialization failure");
      return false;
    }

    auto devicePropertySupportStatus = app->isDevicePropertySupported();
    if (qnn_app::StatusCode::FAILURE != devicePropertySupportStatus) {
      auto createDeviceStatus = app->createDevice();
      if (qnn_app::StatusCode::SUCCESS != createDeviceStatus) {
        app->reportError("Device Creation failure");
        return false;
      }
    }
	
    if (qnn_app::StatusCode::SUCCESS != app->initializeProfiling()) {
      app->reportError("Profiling Initialization failure");
      return false;
    }

    if (qnn_app::StatusCode::SUCCESS != app->registerOpPackages()) {
      app->reportError("Register Op Packages failure");
      return false;
    }

    if (!loadFromCachedBinary ||  (suffix_mode_path == "dlc")) { //issue#23
      if (qnn_app::StatusCode::SUCCESS != app->createContext()) {
        app->reportError("Context Creation failure");
        return false;
      }
      if (qnn_app::StatusCode::SUCCESS != app->composeGraphs()) {
        app->reportError("Graph Prepare failure");
        return false;
      }
      if (qnn_app::StatusCode::SUCCESS != app->finalizeGraphs()) {
        app->reportError("Graph Finalize failure");
        return false;
      }
    } else {
      if (qnn_app::StatusCode::SUCCESS != app->createFromBinary()) {
        app->reportError("Create From Binary failure");
        return false;
      }
    }

    // improve performance.
    if (qnn_app::StatusCode::SUCCESS != app->setupInputAndOutputTensors()) {
      app->reportError("Setup Input and Output Tensors failure");
      return false;
    }

    gs_isGpu = isGpu;
    gs_isCpu = isCpu;	
    app->setIsGpu(isGpu);
    app->setIsCpu(isCpu);
	
    if (loadFromCachedBinary && !isGpu) {
        if (qnn_app::StatusCode::SUCCESS != app->initializePerformance()) {
            app->reportError("Performance initialization failure");
            return false;
        }
    }

    // apply lora Adapter on graph
    if (app->binaryUpdates() &&
        qnn_app::StatusCode::SUCCESS != app->contextApplyBinarySection(QNN_CONTEXT_SECTION_UPDATABLE)) {
        return app->reportError("Binary update/execution failure");
    }

    timerHelper.Print("model_initialize " + model_name);

    putQnnApp(model_name, std::move(app));

    return true;
  }

  return false;
}

bool ModelInferenceEx(std::string model_name, std::string proc_name, std::string share_memory_name,
                      std::vector<uint8_t*>& inputBuffers, std::vector<size_t>& inputSize,
                      std::vector<uint8_t*>& outputBuffers, std::vector<size_t>& outputSize,
                      std::string& perfProfile, size_t graphIndex, size_t share_memory_size=0) {
    bool result = true;

    QNN_INFO("LibAppBuilder::ModelInference: %s \n", model_name.c_str());

    if (!proc_name.empty()) {
        // If proc_name, run the model in that process.
        result = TalkToSvc_Inference(model_name, proc_name, share_memory_name, inputBuffers, inputSize, outputBuffers, outputSize, perfProfile, graphIndex);
        return result;
    }

    TimerHelper timerHelper;

    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);

    if (nullptr == app) {
        // issue#109/#4: model missing/released. Bail out without dereferencing
        // the null app and without re-inserting a null entry into the map.
        QNN_WARN("getQnnInferenceEngine returns null in ModelInferenceEx (model not found or released): %s\n", model_name.c_str());
        return false;
    }

    if (qnn_app::StatusCode::SUCCESS != app->executeGraphsBuffers(inputBuffers, outputBuffers, outputSize, perfProfile, graphIndex, share_memory_size)) {
        app->reportError("Inference failure");
        result = false;
    }

    putQnnApp(model_name, std::move(app));

    timerHelper.Print("model_inference " + model_name);

    return result;
}

bool ModelDestroyEx(std::string model_name, std::string proc_name) {
    QNN_INFO("LibAppBuilder::ModelDestroy: %s \n", model_name.c_str());

    bool result = false;

    if (!proc_name.empty()) {
        // If proc_name, desctroy the model in that process.
        result = TalkToSvc_Destroy(model_name, proc_name);
        return result;
    }

    TimerHelper timerHelper;

    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {
        // issue#109/#4: the model was never registered or has already been
        // destroyed/taken. Do NOT dereference the null app (the original code
        // called app->reportError here, which crashed on a repeated destroy).
        QNN_WARN("ModelDestroy: can't find the model with model_name: %s (already destroyed?)\n", model_name.c_str());
        return false;
    }

    // improve performance.
    if (qnn_app::StatusCode::SUCCESS != app->tearDownInputAndOutputTensors()) {
        app->reportError("Input and Output Tensors destroy failure");
        return false;
    }

    if (qnn_app::StatusCode::SUCCESS != app->destroyPerformance()) {
        app->reportError("Performance destroy failure");
        return false;
    }

    if (qnn_app::StatusCode::SUCCESS != app->freeGraphs()) {
        app->reportError("Free graphs failure");
        return false;
    }

    if (qnn_app::StatusCode::SUCCESS != app->freeContext()) {
        app->reportError("Context Free failure");
        return false;
    }

    auto devicePropertySupportStatus = app->isDevicePropertySupported();
    if (qnn_app::StatusCode::FAILURE != devicePropertySupportStatus) {
        auto freeDeviceStatus = app->freeDevice();
        if (qnn_app::StatusCode::SUCCESS != freeDeviceStatus) {
            app->reportError("Device Free failure");
            return false;
        }
    }
    timerHelper.Print("model_destroy " + model_name);

    return true;
}


/////////////////////////////////////////////////////////////////////////////
/// Class LibAppBuilder implementation.
/////////////////////////////////////////////////////////////////////////////

bool LibAppBuilder::ModelInitialize(const std::string& model_name, const std::string& proc_name, const std::string& model_path,
                                    const std::string& backend_lib_path, const std::string& system_lib_path,
                                    bool async, const std::string& input_data_type, const std::string& output_data_type, uint32_t deviceID, std::string coreIdsStr) {
    if (!proc_name.empty()) {   // Create process and save process info & model name to map, load model in new process.
        return TalkToSvc_Initialize(model_name, proc_name, model_path, backend_lib_path, system_lib_path, async, input_data_type, output_data_type);
    }
    return false;
}

bool LibAppBuilder::ModelInitialize(const std::string& model_name, const std::string& model_path,
                                    const std::string& backend_lib_path, const std::string& system_lib_path,
                                    bool async, const std::string& input_data_type, const std::string& output_data_type, uint32_t deviceID, std::string coreIdsStr) {
    std::vector<LoraAdapter> Adapters = std::vector<LoraAdapter>();
    return ModelInitializeEx(model_name, "", model_path, backend_lib_path, system_lib_path, Adapters, async, input_data_type, output_data_type, deviceID, coreIdsStr);   
}

bool LibAppBuilder::ModelInitialize(const std::string& model_name, const std::string& model_path,
                                    const std::string& backend_lib_path, const std::string& system_lib_path,
                                    std::vector<LoraAdapter>& lora_adapters,
                                    bool async, const std::string& input_data_type, const std::string& output_data_type, uint32_t deviceID, std::string coreIdsStr) {
    return ModelInitializeEx(model_name, "", model_path, backend_lib_path, system_lib_path, lora_adapters, async, input_data_type, output_data_type, deviceID, coreIdsStr);
}

bool LibAppBuilder::ModelInference(std::string model_name, std::string proc_name, std::string share_memory_name,
                                   std::vector<uint8_t*>& inputBuffers, std::vector<size_t>& inputSize,
                                   std::vector<uint8_t*>& outputBuffers, std::vector<size_t>& outputSize,
                                   std::string& perfProfile, size_t graphIndex) {
    if (!proc_name.empty()) {   // If proc_name, run the model in that process.
        return TalkToSvc_Inference(model_name, proc_name, share_memory_name, inputBuffers, inputSize, outputBuffers, outputSize, perfProfile, graphIndex);
    }
    return false;
}

bool LibAppBuilder::ModelInference(std::string model_name, std::vector<uint8_t*>& inputBuffers, 
                                   std::vector<uint8_t*>& outputBuffers, std::vector<size_t>& outputSize,
                                   std::string& perfProfile, size_t graphIndex, size_t share_memory_size){
    std::vector<size_t> inputSize;
    return ModelInferenceEx(model_name, "", "", inputBuffers, inputSize, outputBuffers, outputSize, perfProfile, graphIndex, share_memory_size);
}

bool LibAppBuilder::ModelApplyBinaryUpdate(const std::string model_name, std::vector<LoraAdapter>& lora_adapters) {
    bool result = true;
    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {
        // issue#109/#4: model missing/released. Bail out without dereferencing
        // the null app and without re-inserting a null entry into the map.
        QNN_WARN("Apply binary update failure: %s (model not found or released)\n", model_name.c_str());
        return false;
    }

    app->update_m_lora_adapters(lora_adapters);

    QNN_INFO("Applying Binary update on the graph");

    if (qnn_app::StatusCode::SUCCESS != app->contextApplyBinarySection(QNN_CONTEXT_SECTION_UPDATABLE)) {
        app->reportError("Binary update failure");
        result = false;
    }

    putQnnApp(model_name, std::move(app));

    return result;
}

bool LibAppBuilder::ModelDestroy(std::string model_name, std::string proc_name) {
    if (!proc_name.empty()) {   // If proc_name, desctroy the model in that process.
        return TalkToSvc_Destroy(model_name, proc_name);
    }
    return false;
}

bool LibAppBuilder::ModelDestroy(std::string model_name) {
    return ModelDestroyEx(model_name, "");
}

bool LibAppBuilder::CreateShareMemory(std::string share_memory_name, size_t share_memory_size) {
    return CreateShareMem(share_memory_name, share_memory_size);
}

bool LibAppBuilder::DeleteShareMemory(std::string share_memory_name) {
    return DeleteShareMem(share_memory_name);
}

// issue#24
std::vector<std::vector<size_t>> LibAppBuilder::getOutputShapes(std::string model_name){
    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {  // issue#109/#4: guard against released/missing context.
        QNN_WARN("getOutputShapes: model not found or released: %s\n", model_name.c_str());
        return {};
    }
    m_outputShapes = app->getOutputShapes();
    putQnnApp(model_name, std::move(app));
    return m_outputShapes;
};

std::vector<std::vector<size_t>> LibAppBuilder::getInputShapes(std::string model_name){
    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {  // issue#109/#4
        QNN_WARN("getInputShapes: model not found or released: %s\n", model_name.c_str());
        return {};
    }
    m_inputShapes = app->getInputShapes();
    putQnnApp(model_name, std::move(app));
    return m_inputShapes;
};

std::vector<std::string> LibAppBuilder::getInputDataType(std::string model_name){
    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {  // issue#109/#4
        QNN_WARN("getInputDataType: model not found or released: %s\n", model_name.c_str());
        return {};
    }
    m_inputDataType = app->getInputDataType();
    putQnnApp(model_name, std::move(app));
    return m_inputDataType;
};

std::vector<std::string> LibAppBuilder::getOutputDataType(std::string model_name){
    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {  // issue#109/#4
        QNN_WARN("getOutputDataType: model not found or released: %s\n", model_name.c_str());
        return {};
    }
    m_outputDataType = app->getOutputDataType();
    putQnnApp(model_name, std::move(app));
    return m_outputDataType;
};

std::string LibAppBuilder::getGraphName(std::string model_name){
    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {  // issue#109/#4
        QNN_WARN("getGraphName: model not found or released: %s\n", model_name.c_str());
        return {};
    }
    m_graphName = app->getGraphName();
    putQnnApp(model_name, std::move(app));
    return m_graphName;
};

std::vector<std::string> LibAppBuilder::getInputName(std::string model_name){
    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {  // issue#109/#4
        QNN_WARN("getInputName: model not found or released: %s\n", model_name.c_str());
        return {};
    }
    m_inputName = app->getInputName();
    putQnnApp(model_name, std::move(app));
    return m_inputName;
};

std::vector<std::string> LibAppBuilder::getOutputName(std::string model_name){
    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {  // issue#109/#4
        QNN_WARN("getOutputName: model not found or released: %s\n", model_name.c_str());
        return {};
    }
    m_outputName = app->getOutputName();
    putQnnApp(model_name, std::move(app));
    return m_outputName;
};
//proc
std::vector<std::vector<size_t>> LibAppBuilder::getOutputShapes(std::string model_name, std::string proc_name){
    ::ModelInfo_t m_moduleInfo  = getModelInfo(model_name, proc_name,  "os");
    return m_moduleInfo.outputShapes;
};

std::vector<std::vector<size_t>> LibAppBuilder::getInputShapes(std::string model_name, std::string proc_name){
    ::ModelInfo_t m_moduleInfo = getModelInfo(model_name, proc_name,  "is");
    return m_moduleInfo.inputShapes;
};

std::vector<std::string> LibAppBuilder::getInputDataType(std::string model_name, std::string proc_name){
    ::ModelInfo_t m_moduleInfo  = getModelInfo(model_name, proc_name,  "id");
    return m_moduleInfo.inputDataType;
};

std::vector<std::string> LibAppBuilder::getOutputDataType(std::string model_name, std::string proc_name){
    ::ModelInfo_t m_moduleInfo  = getModelInfo(model_name, proc_name,  "od");
    return m_moduleInfo.outputDataType;
};

std::string LibAppBuilder::getGraphName(std::string model_name, std::string proc_name){
    ::ModelInfo_t m_moduleInfo  = getModelInfo(model_name, proc_name,  "gn");
    return m_moduleInfo.graphName;
};

std::vector<std::string> LibAppBuilder::getInputName(std::string model_name, std::string proc_name){
    ::ModelInfo_t m_moduleInfo  = getModelInfo(model_name, proc_name,  "in");
    return m_moduleInfo.inputName;
};

std::vector<std::string> LibAppBuilder::getOutputName(std::string model_name, std::string proc_name){
    ::ModelInfo_t m_moduleInfo  = getModelInfo(model_name, proc_name,  "on");
    return m_moduleInfo.outputName;
};

ModelInfo_t LibAppBuilder::getModelInfo(std::string model_name, std::string proc_name, std::string input) {
    ModelInfo_t output;
    if (!proc_name.empty()) {   // If proc_name, run the model in that process.
        output = TalkToSvc_getModelInfo(model_name, proc_name, input);
    }
    return output;
}

ModelInfo_t LibAppBuilder::getModelInfo(std::string model_name, std::string input) {
    return getModelInfoExt(model_name, input);
}
ModelInfo_t LibAppBuilder::getModelInfoExt(std::string model_name, std::string input) {
    ModelInfo_t info;

    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {
        // issue#109/#4: model missing/released. Return empty info without
        // dereferencing the null app or re-inserting a null map entry.
        QNN_WARN("getModelInfoExt failure: %s (model not found or released)\n", model_name.c_str());
        return info;
    }

    if (input == "is") {
        info.inputShapes = app->getInputShapes();
    } else if (input == "id") {
        info.inputDataType = app->getInputDataType();
    } else if (input == "os") {
        info.outputShapes = app->getOutputShapes();
    } else if (input == "od") {
        info.outputDataType = app->getOutputDataType();
    } else if (input == "in") {
        info.inputName = app->getInputName();
    } else if (input == "on") {
        info.outputName = app->getOutputName();
    } else if (input == "gn") {
        info.graphName = app->getGraphName();
    } else {
        printf("wrong input in LibAppBuilder::getModelInfoExt: %s\n", input.c_str());
        app->reportError("getModelInfoExt failure");
        // Put the app back before returning so the model is not lost.
        putQnnApp(model_name, std::move(app));
        return info;
    }
    putQnnApp(model_name, std::move(app));

    return info;
}

uint64_t LibAppBuilder::getProfilingEvent(std::string model_name, uint32_t eventType){
    uint64_t eventValue = 0;
    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {  // issue#109/#4
        QNN_WARN("getProfilingEvent: model not found or released: %s\n", model_name.c_str());
        return 0;
    }
    eventValue = app->getProfilingEvent(eventType);
    putQnnApp(model_name, std::move(app));
    return eventValue;
}

int main(int argc, char** argv) {

    return EXIT_SUCCESS;
}
