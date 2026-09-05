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
#include <atomic>
#include <condition_variable>
#include <optional>
#include <type_traits>
#include <utility>
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

// ---------------------------------------------------------------------------
// issue#97: fork()-safety — detect forked children and prevent them from
// using or tearing down the parent's inherited QNN/FastRPC state.
// ---------------------------------------------------------------------------
#ifdef __linux__
#include <unistd.h>
// PID of the process that first initialized the QNN backend. A forked child
// inherits this value; comparing it with getpid() detects the fork.
static pid_t sg_qnnOwnerPid = -1;

static bool isForkedChildWithInheritedQnnState() noexcept {
    return sg_qnnOwnerPid > 0 && getpid() != sg_qnnOwnerPid;
}
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

// Intentionally process-lifetime allocated (never enters CRT static
// destruction).  If atexit / ShutdownAllModels() runs, the map is drained
// and engines are properly torn down.  If atexit does NOT run (os._exit,
// fatal signal, etc.), the map leaks harmlessly — the OS reclaims the
// address space on process termination, and we avoid calling QNN APIs in
// the uncontrolled DLL/CRT teardown phase where provider state is
// indeterminate.  This is safer than letting ~unordered_map destroy
// engines whose QNN backend may already be gone.
using EngineMap = std::unordered_map<std::string,
                                     std::unique_ptr<qnn_app::QnnInferenceEngine>>;

// These stores deliberately have process lifetime so their destructors cannot
// call QNN during CRT/DLL teardown.  They may allocate; callers must therefore
// initialize them during normal module startup, not from an atexit callback.
static EngineMap& sg_model_map() {
    static EngineMap* map = new EngineMap();
    return *map;
}
static std::mutex& sg_model_map_mutex() {
    static std::mutex* mtx = new std::mutex();
    return *mtx;
}

// ---------------------------------------------------------------------------
// Process-exit safety: runtime lifecycle + unified teardown
// ---------------------------------------------------------------------------
enum class RuntimeState { Running, ShuttingDown, Shutdown };

struct RuntimeLifecycle {
    std::mutex mutex;
    std::condition_variable cv;
    std::atomic<RuntimeState> state{RuntimeState::Running};
    std::size_t activeOperations{0};
    ShutdownAllModelsStatus shutdownResult{ShutdownAllModelsStatus::Failed};
};

// Process-lifetime allocated — same rationale as the engine registry.
static RuntimeLifecycle& sg_runtime() {
    static RuntimeLifecycle* rt = new RuntimeLifecycle();
    return *rt;
}

// Counts guards held by this thread.  Shutdown must not wait for an operation
// that is blocked in that same shutdown call.
static thread_local std::size_t sg_runtimeOperationDepth{0};

void InitializeRuntimeLifecycleStores() {
    (void)sg_model_map();
    (void)sg_model_map_mutex();
    (void)sg_runtime();
}

// Covers one extract-use-return operation. Acquire before extracting or
// creating an engine; keep it alive until putQnnApp() has returned (or the
// extracted unique_ptr has been destroyed). This lets shutdown swap the
// registry without ever nesting lifecycle and registry mutexes.
class RuntimeOperationGuard {
public:
    static std::optional<RuntimeOperationGuard> acquire() noexcept {
        auto& rt = sg_runtime();
        if (rt.state.load(std::memory_order_acquire) != RuntimeState::Running) {
            return std::nullopt;
        }
        std::lock_guard<std::mutex> lock(rt.mutex);
        if (rt.state.load(std::memory_order_relaxed) != RuntimeState::Running) {
            return std::nullopt;
        }
        ++rt.activeOperations;
        ++sg_runtimeOperationDepth;
        return RuntimeOperationGuard{};
    }

    RuntimeOperationGuard(RuntimeOperationGuard&& other) noexcept
        : m_active(std::exchange(other.m_active, false)) {}
    RuntimeOperationGuard& operator=(RuntimeOperationGuard&&) = delete;
    RuntimeOperationGuard(const RuntimeOperationGuard&) = delete;
    RuntimeOperationGuard& operator=(const RuntimeOperationGuard&) = delete;

    ~RuntimeOperationGuard() noexcept {
        if (!m_active) return;
        auto& rt = sg_runtime();
        {
            std::lock_guard<std::mutex> lock(rt.mutex);
            --rt.activeOperations;
            --sg_runtimeOperationDepth;
        }
        rt.cv.notify_all();
    }

private:
    RuntimeOperationGuard() = default;
    bool m_active{true};
};

/// Destroy one engine — delegates to the engine's own idempotent shutdown().
static bool DestroyEngineSafely(
    const std::string& /*model_name*/,
    std::unique_ptr<qnn_app::QnnInferenceEngine> engine) noexcept
{
    if (!engine) return true;
    const bool succeeded = engine->shutdown() == qnn_app::StatusCode::SUCCESS;
    engine.reset();
    return succeeded;
}

static_assert(
    noexcept(std::declval<EngineMap&>().swap(std::declval<EngineMap&>())),
    "EngineMap::swap must be noexcept for shutdown safety");

/// Drain the engine registry and perform full teardown. Operations which have
/// extracted an engine finish before the registry is swapped.
static ShutdownAllModelsStatus ShutdownAllModelsImpl()
{
    if (sg_runtimeOperationDepth != 0) {
        return ShutdownAllModelsStatus::InActiveOperation;
    }

    EngineMap engines;
    auto& rt = sg_runtime();
    {
        std::unique_lock<std::mutex> lock(rt.mutex);
        const auto state = rt.state.load(std::memory_order_relaxed);
        if (state == RuntimeState::Shutdown) return rt.shutdownResult;
        if (state == RuntimeState::ShuttingDown) {
            rt.cv.wait(lock, [&rt] {
                return rt.state.load(std::memory_order_acquire) == RuntimeState::Shutdown;
            });
            return rt.shutdownResult;
        }
        rt.state.store(RuntimeState::ShuttingDown, std::memory_order_release);
        rt.cv.wait(lock, [&rt] { return rt.activeOperations == 0; });
    }

    // Lock invariant: lifecycle and registry mutexes are never held together.
    // Guards prevent extraction/insertion after state becomes ShuttingDown.
    {
        std::lock_guard<std::mutex> lock(sg_model_map_mutex());
        engines.swap(sg_model_map());
    }

    auto result = ShutdownAllModelsStatus::Completed;
    for (auto& [name, engine] : engines) {
        if (!DestroyEngineSafely(name, std::move(engine))) {
            result = ShutdownAllModelsStatus::Failed;
        }
    }

    {
        std::lock_guard<std::mutex> lock(rt.mutex);
        rt.shutdownResult = result;
        rt.state.store(RuntimeState::Shutdown, std::memory_order_release);
    }
    rt.cv.notify_all();
    return result;
}

extern "C" ShutdownAllModelsStatus ShutdownAllModels() noexcept
{
    try {
        return ShutdownAllModelsImpl();
    } catch (...) {
        auto& rt = sg_runtime();
        {
            std::lock_guard<std::mutex> lock(rt.mutex);
            rt.shutdownResult = ShutdownAllModelsStatus::Failed;
            rt.state.store(RuntimeState::Shutdown, std::memory_order_release);
        }
        rt.cv.notify_all();
        return ShutdownAllModelsStatus::Failed;
    }
}
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

#ifdef __linux__
  // issue#97: record which process owns the backend state loaded above.
  if (sg_qnnOwnerPid == -1) {
      sg_qnnOwnerPid = getpid();
  }
#endif
  std::unique_ptr<qnn_app::QnnInferenceEngine> app(new qnn_app::QnnInferenceEngine(qnnFunctionPointers, "null", opPackagePaths, sg_backendHandle, "null",
                                                                             debug, parsedOutputDataType, parsedInputDataType, sg_parsedProfilingLevel,
                                                                             dumpOutputs, cachedBinaryPath2, saveBinaryName, lora_adapters, cachedBinaryPath2, multiCoreDeviceConfig));
    return app;
}

}  // namespace libappbuilder
}  // namespace tools
}  // namespace qnn


std::unique_ptr<qnn_app::QnnInferenceEngine> getQnnInferenceEngine(std::string model_name) {
  std::lock_guard<std::mutex> lk(sg_model_map_mutex());
  auto it = sg_model_map().find(model_name);
  if (it != sg_model_map().end()) {
    if (it->second) {
      auto app = std::move(it->second);
      sg_model_map().erase(it);
      return app;
    }
  }
  return nullptr;
}

// Symmetric counterpart to getQnnInferenceEngine. The caller's
// RuntimeOperationGuard prevents shutdown from swapping the registry until
// this insertion completes, so this function only ever acquires registry state.
// When insertion fails, the by-value app parameter is destroyed on return;
// QnnInferenceEngine's destructor invokes its idempotent shutdown().
bool putQnnApp(std::string model_name,
               std::unique_ptr<qnn_app::QnnInferenceEngine> app) noexcept {
  if (!app) return false;
  try {
    std::lock_guard<std::mutex> lock(sg_model_map_mutex());
    auto [it, inserted] = sg_model_map().try_emplace(
        std::move(model_name), std::move(app));
    return inserted;
  } catch (...) {
    return false;
  }
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
  auto operation = RuntimeOperationGuard::acquire();
  if (!operation) return false;
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
    auto operation = RuntimeOperationGuard::acquire();
    if (!operation) {
        QNN_ERR("SetPerfProfileGlobal: runtime is shutting down.\n");
        return false;
    }
#ifdef __linux__
    if (isForkedChildWithInheritedQnnState()) {
        QNN_WARN("SetPerfProfileGlobal: skipping in fork()ed child (issue#97).\n");
        return true;
    }
#endif
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
    auto operation = RuntimeOperationGuard::acquire();
    if (!operation) return false;
#ifdef __linux__
    if (isForkedChildWithInheritedQnnState()) {
        QNN_WARN("RelPerfProfileGlobal: skipping in fork()ed child (issue#97).\n");
        return true;
    }
#endif
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
                       bool async, const std::string& input_data_type, const std::string& output_data_type, uint32_t deviceID=0, std::string coreIdsStr="", const std::vector<std::string>& enable_graphs={}) {
  auto operation = RuntimeOperationGuard::acquire();
  if (!operation) {
      QNN_ERR("ModelInitializeEx: runtime is shutting down, cannot create new engine.\n");
      return false;
  }
  QNN_INFO("LibAppBuilder::ModelInitialize: %s \n", model_name.c_str());

#ifdef __linux__
  // issue#97: reject QNN operations in a fork()ed child that inherited the
  // parent's backend state. The inherited QNN/FastRPC handles, device fds and
  // library static state cannot be reused or rebuilt safely in the child.
  if (isForkedChildWithInheritedQnnState()) {
      QNN_ERROR("ModelInitializeEx: QNN was initialized before fork(). The inherited "
                "QNN/FastRPC state cannot be used safely in this child process. "
                "Use multiprocessing with the 'spawn' start method, or create "
                "the child before initializing QNN.\n");
      return false;
  }
#endif

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

    // Must be set before createFromBinary(), which is where the selection is
    // turned into a context config.
    app->setEnabledGraphs(enable_graphs);

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

    if (!putQnnApp(model_name, std::move(app))) {
      QNN_ERR("ModelInitializeEx: failed to register initialized model: %s\n", model_name.c_str());
      return false;
    }

    return true;
  }

  return false;
}

bool ModelInferenceEx(std::string model_name, std::string proc_name, std::string share_memory_name,
                      std::vector<uint8_t*>& inputBuffers, std::vector<size_t>& inputSize,
                      std::vector<uint8_t*>& outputBuffers, std::vector<size_t>& outputSize,
                      std::string& perfProfile, size_t graphIndex, size_t share_memory_size=0) {
    auto operation = RuntimeOperationGuard::acquire();
    if (!operation) {
        QNN_ERR("ModelInferenceEx: runtime is shutting down.\n");
        return false;
    }
    bool result = true;

    QNN_INFO("LibAppBuilder::ModelInference: %s \n", model_name.c_str());

#ifdef __linux__
    if (isForkedChildWithInheritedQnnState()) {
        QNN_ERROR("ModelInferenceEx: cannot run inference in a fork()ed child "
                  "with inherited QNN state (issue#97).\n");
        return false;
    }
#endif

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

    if (!putQnnApp(model_name, std::move(app))) {
        QNN_ERR("ModelInferenceEx: failed to restore model after inference: %s\n", model_name.c_str());
        return false;
    }

    timerHelper.Print("model_inference " + model_name);

    return result;
}

bool ModelDestroyEx(std::string model_name, std::string proc_name) {
    QNN_INFO("LibAppBuilder::ModelDestroy: %s \n", model_name.c_str());

#ifdef __linux__
    if (isForkedChildWithInheritedQnnState()) {
        QNN_WARN("ModelDestroyEx: skipping teardown in fork()ed child to protect "
                 "the parent's QNN/FastRPC session (issue#97).\n");
        return true;  // no-op success — resources leak intentionally
    }
#endif

    auto operation = RuntimeOperationGuard::acquire();
    if (!operation) {
        QNN_ERR("ModelDestroyEx: runtime is shutting down.\n");
        return false;
    }
    bool result = false;

    if (!proc_name.empty()) {
        // If proc_name, desctroy the model in that process.
        result = TalkToSvc_Destroy(model_name, proc_name);
        return result;
    }

    TimerHelper timerHelper;

    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {
        QNN_WARN("ModelDestroy: can't find the model with model_name: %s (already destroyed?)\n", model_name.c_str());
        return false;
    }

    // Unified teardown — the engine's own idempotent shutdown() handles
    // the complete resource release sequence.  No need to call individual
    // free functions here; that was the source of double-free risks when
    // the destructor also released the same handles.
    auto status = app->shutdown();
    timerHelper.Print("model_destroy " + model_name);

    return (qnn_app::StatusCode::SUCCESS == status);
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
                                    bool async, const std::string& input_data_type, const std::string& output_data_type, uint32_t deviceID, std::string coreIdsStr, const std::vector<std::string>& enable_graphs) {
    std::vector<LoraAdapter> Adapters = std::vector<LoraAdapter>();
    return ModelInitializeEx(model_name, "", model_path, backend_lib_path, system_lib_path, Adapters, async, input_data_type, output_data_type, deviceID, coreIdsStr, enable_graphs);
}

bool LibAppBuilder::ModelInitialize(const std::string& model_name, const std::string& model_path,
                                    const std::string& backend_lib_path, const std::string& system_lib_path,
                                    std::vector<LoraAdapter>& lora_adapters,
                                    bool async, const std::string& input_data_type, const std::string& output_data_type, uint32_t deviceID, std::string coreIdsStr, const std::vector<std::string>& enable_graphs) {
    return ModelInitializeEx(model_name, "", model_path, backend_lib_path, system_lib_path, lora_adapters, async, input_data_type, output_data_type, deviceID, coreIdsStr, enable_graphs);
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

std::string LibAppBuilder::ModelInferenceAsync(std::string model_name, std::string proc_name,
                                               std::string share_memory_name,
                                               std::vector<uint8_t*>& inputBuffers,
                                               std::vector<size_t>& inputSize,
                                               std::string& perfProfile, size_t graphIndex) {
    if (proc_name.empty()) {
        QNN_ERR("ModelInferenceAsync: proc_name is required.\n");
        return "";
    }
    return TalkToSvc_InferenceAsync(model_name, proc_name, share_memory_name,
                                    inputBuffers, inputSize, perfProfile, graphIndex);
}

bool LibAppBuilder::ModelWaitInference(const std::string& request_id,
                                       const std::string& proc_name,
                                       const std::string& share_memory_name,
                                       std::vector<uint8_t*>& outputBuffers,
                                       std::vector<size_t>& outputSize) {
    if (proc_name.empty()) {
        QNN_ERR("ModelWaitInference: proc_name is required.\n");
        return false;
    }
    return TalkToSvc_WaitInference(request_id, proc_name, share_memory_name,
                                   outputBuffers, outputSize);
}

bool LibAppBuilder::ModelApplyBinaryUpdate(const std::string model_name, std::vector<LoraAdapter>& lora_adapters) {
    auto operation = RuntimeOperationGuard::acquire();
    if (!operation) {
        QNN_ERR("ModelApplyBinaryUpdate: runtime is shutting down.\n");
        return false;
    }
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

    if (!putQnnApp(model_name, std::move(app))) {
        QNN_ERR("ModelApplyBinaryUpdate: failed to restore model: %s\n", model_name.c_str());
        return false;
    }

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
std::vector<std::vector<size_t>> LibAppBuilder::getOutputShapes(std::string model_name, size_t graphIdx){
    auto operation = RuntimeOperationGuard::acquire();
    if (!operation) return {};
    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {  // issue#109/#4: guard against released/missing context.
        QNN_WARN("getOutputShapes: model not found or released: %s\n", model_name.c_str());
        return {};
    }
    m_outputShapes = app->getOutputShapes(graphIdx);
    if (!putQnnApp(model_name, std::move(app))) {
        QNN_ERR("getOutputShapes: failed to restore model: %s\n", model_name.c_str());
        return {};
    }
    return m_outputShapes;
};

std::vector<std::vector<size_t>> LibAppBuilder::getInputShapes(std::string model_name, size_t graphIdx){
    auto operation = RuntimeOperationGuard::acquire();
    if (!operation) return {};
    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {  // issue#109/#4
        QNN_WARN("getInputShapes: model not found or released: %s\n", model_name.c_str());
        return {};
    }
    m_inputShapes = app->getInputShapes(graphIdx);
    if (!putQnnApp(model_name, std::move(app))) {
        QNN_ERR("getInputShapes: failed to restore model: %s\n", model_name.c_str());
        return {};
    }
    return m_inputShapes;
};

std::vector<std::string> LibAppBuilder::getInputDataType(std::string model_name, size_t graphIdx){
    auto operation = RuntimeOperationGuard::acquire();
    if (!operation) return {};
    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {  // issue#109/#4
        QNN_WARN("getInputDataType: model not found or released: %s\n", model_name.c_str());
        return {};
    }
    m_inputDataType = app->getInputDataType(graphIdx);
    if (!putQnnApp(model_name, std::move(app))) {
        QNN_ERR("getInputDataType: failed to restore model: %s\n", model_name.c_str());
        return {};
    }
    return m_inputDataType;
};

std::vector<std::string> LibAppBuilder::getOutputDataType(std::string model_name, size_t graphIdx){
    auto operation = RuntimeOperationGuard::acquire();
    if (!operation) return {};
    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {  // issue#109/#4
        QNN_WARN("getOutputDataType: model not found or released: %s\n", model_name.c_str());
        return {};
    }
    m_outputDataType = app->getOutputDataType(graphIdx);
    if (!putQnnApp(model_name, std::move(app))) {
        QNN_ERR("getOutputDataType: failed to restore model: %s\n", model_name.c_str());
        return {};
    }
    return m_outputDataType;
};

std::string LibAppBuilder::getGraphName(std::string model_name, size_t graphIdx){
    auto operation = RuntimeOperationGuard::acquire();
    if (!operation) return {};
    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {  // issue#109/#4
        QNN_WARN("getGraphName: model not found or released: %s\n", model_name.c_str());
        return {};
    }
    m_graphName = app->getGraphName(graphIdx);
    if (!putQnnApp(model_name, std::move(app))) {
        QNN_ERR("getGraphName: failed to restore model: %s\n", model_name.c_str());
        return {};
    }
    return m_graphName;
};

std::vector<std::string> LibAppBuilder::getInputName(std::string model_name, size_t graphIdx){
    auto operation = RuntimeOperationGuard::acquire();
    if (!operation) return {};
    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {  // issue#109/#4
        QNN_WARN("getInputName: model not found or released: %s\n", model_name.c_str());
        return {};
    }
    m_inputName = app->getInputName(graphIdx);
    if (!putQnnApp(model_name, std::move(app))) {
        QNN_ERR("getInputName: failed to restore model: %s\n", model_name.c_str());
        return {};
    }
    return m_inputName;
};

std::vector<std::string> LibAppBuilder::getOutputName(std::string model_name, size_t graphIdx){
    auto operation = RuntimeOperationGuard::acquire();
    if (!operation) return {};
    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {  // issue#109/#4
        QNN_WARN("getOutputName: model not found or released: %s\n", model_name.c_str());
        return {};
    }
    m_outputName = app->getOutputName(graphIdx);
    if (!putQnnApp(model_name, std::move(app))) {
        QNN_ERR("getOutputName: failed to restore model: %s\n", model_name.c_str());
        return {};
    }
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

    auto operation = RuntimeOperationGuard::acquire();
    if (!operation) return info;
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
        if (!putQnnApp(model_name, std::move(app))) {
            QNN_ERR("getModelInfoExt: failed to restore model: %s\n", model_name.c_str());
        }
        return info;
    }
    if (!putQnnApp(model_name, std::move(app))) {
        QNN_ERR("getModelInfoExt: failed to restore model: %s\n", model_name.c_str());
        return {};
    }

    return info;
}

uint64_t LibAppBuilder::getProfilingEvent(std::string model_name, uint32_t eventType){
    auto operation = RuntimeOperationGuard::acquire();
    if (!operation) return 0;
    uint64_t eventValue = 0;
    std::unique_ptr<qnn_app::QnnInferenceEngine> app = getQnnInferenceEngine(model_name);
    if (nullptr == app) {  // issue#109/#4
        QNN_WARN("getProfilingEvent: model not found or released: %s\n", model_name.c_str());
        return 0;
    }
    eventValue = app->getProfilingEvent(eventType);
    if (!putQnnApp(model_name, std::move(app))) {
        QNN_ERR("getProfilingEvent: failed to restore model: %s\n", model_name.c_str());
        return 0;
    }
    return eventValue;
}

int main(int argc, char** argv) {

    return EXIT_SUCCESS;
}
