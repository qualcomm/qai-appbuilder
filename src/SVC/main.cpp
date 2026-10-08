//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "Utils/Utils.hpp"
#include "Lora.hpp"
#include <string>
#include <sstream>
#include <map>
#include <future>
#include <mutex>
#include <unordered_map>
#include <atomic>


// ============================== Service / QAIAppSvc ============================== //

#define ACTION_OK        "OK"
#define ACTION_FAILED    "Failed"

LibAppBuilder g_LibAppBuilder;

// Tracks pending async model loads: model_name -> future<bool>
static std::mutex g_async_futures_mutex;
static std::unordered_map<std::string, std::future<bool>> g_async_futures;

// Async inference result storage
struct InferResult {
    bool        success = false;
    std::string offsets;   // strOffsetArray
    std::string sizes;     // strSizeArray
};

static std::atomic<uint64_t> g_request_counter{0};
static std::mutex g_infer_results_mutex;
static std::unordered_map<std::string, std::future<InferResult>> g_infer_futures;

// Server side: map the shared-memory region for this inference.
//   Windows : open the named mapping created by the client.
//   POSIX   : adopt the fd just received over the command channel (SCM_RIGHTS).
// registry_key: the key used in sg_share_mem_map (defaults to share_memory_name).
//   For async inferences, pass a unique key to avoid aliasing when the same
//   share_memory_name is used concurrently by multiple background threads.
void* OpenShareMem(std::string share_memory_name, size_t share_memory_size,
                   ipc::ShmHandle posixFd = ipc::kInvalidShm(),
                   const std::string& registry_key = "") {
    const std::string& reg_key = registry_key.empty() ? share_memory_name : registry_key;
    std::unique_ptr<ipc::SharedRegion> region;
#ifdef _WIN32
    (void)posixFd;
    // Windows: OS-level name stays share_memory_name; only the map key differs.
    region = ipc::SharedRegion::OpenByName(share_memory_name, share_memory_size);
#else
    region = ipc::SharedRegion::OpenFromHandle(posixFd, share_memory_size);
#endif
    if (!region) {
        return nullptr;
    }

    void* lpBase = region->Base();
    if (!RegisterShareMem(reg_key, std::move(region))) {
        return nullptr;
    }
    return lpBase;
}

void CloseShareMem(std::string share_memory_name) {
    auto it = sg_share_mem_map.find(share_memory_name);
    if (it != sg_share_mem_map.end()) {
        sg_share_mem_map.erase(it);   // unique_ptr dtor unmaps / closes the region
    }
    else {
        QNN_ERR("CloseShareMem::Can't find share memory%s.\n", share_memory_name.c_str());
    }
}

void ModelLoad(std::string cmdBuf, ipc::IpcChannel* channel) {
    Print_MemInfo("ModelLoad Start.");

    std::vector<std::string> commands;
    split_string(commands, cmdBuf, ';');

    std::string model_name                  = commands[0];
    std::string model_path                  = commands[1];
    std::string backend_lib_path            = commands[2];
    std::string system_lib_path             = commands[3];
    std::string async_str                   = commands[4];
    std::string input_data_type             = commands[5];
    std::string output_data_type            = commands[6];

    bool async = (async_str == "async");

    if (async) {
        // Launch model initialization on a background thread.
        // ModelRun will wait for it to finish before running inference.
        std::vector<LoraAdapter> Adapters;
        std::future<bool> fut = std::async(std::launch::async, [=]() mutable -> bool {
            Print_MemInfo("ModelLoad(async)::ModelInitialize Start.");
            QNN_INF("ModelLoad(async)::ModelInitialize::Model name %s\n", model_name.c_str());
            bool bSuccess = g_LibAppBuilder.ModelInitialize(model_name.c_str(), model_path, backend_lib_path, system_lib_path, Adapters, false, input_data_type, output_data_type);
            QNN_INF("ModelLoad(async)::ModelInitialize End ret = %d\n", bSuccess);
            Print_MemInfo("ModelLoad(async)::ModelInitialize End.");
            return bSuccess;
        });

        {
            std::lock_guard<std::mutex> lk(g_async_futures_mutex);
            g_async_futures[model_name] = std::move(fut);
        }
        // Do not send a response — client does not wait in async mode.
    } else {
        Print_MemInfo("ModelLoad::ModelInitialize Start.");
        QNN_INF("ModelLoad::ModelInitialize::Model name %s\n", model_name.c_str());
        std::vector<LoraAdapter> Adapters;
        BOOL bSuccess = g_LibAppBuilder.ModelInitialize(model_name.c_str(), model_path, backend_lib_path, system_lib_path, Adapters, false, input_data_type, output_data_type);
        QNN_INF("ModelLoad::ModelInitialize End ret = %d\n", bSuccess);
        Print_MemInfo("ModelLoad::ModelInitialize End.");

        if (bSuccess) {
            channel->Write(ACTION_OK, strlen(ACTION_OK) + 1);
        } else {
            channel->Write(ACTION_FAILED, strlen(ACTION_FAILED) + 1);
        }
    }
}

// If there is a pending async load for this model, wait for it to finish.
// Returns false if the load failed; true if load succeeded or was already done.
static bool WaitForAsyncLoad(const std::string& model_name) {
    std::future<bool> fut;
    {
        std::lock_guard<std::mutex> lk(g_async_futures_mutex);
        auto it = g_async_futures.find(model_name);
        if (it != g_async_futures.end()) {
            fut = std::move(it->second);
            g_async_futures.erase(it);
        }
    }
    if (fut.valid()) {
        return fut.get();
    }
    return true;  // no pending async load — nothing to wait for
}

// Wait for all pending async model loads to finish (used by perf commands that
// require the backend handle to be initialized).
static void WaitForAllAsyncLoads() {
    std::unordered_map<std::string, std::future<bool>> pending;
    {
        std::lock_guard<std::mutex> lk(g_async_futures_mutex);
        pending = std::move(g_async_futures);
    }
    for (auto& kv : pending) {
        if (kv.second.valid()) {
            kv.second.get();
        }
    }
}

void ModelRun(std::string cmdBuf, ipc::IpcChannel* channel, ipc::ShmHandle posixFd) {
    Print_MemInfo("ModelRun Start.");

    std::vector<std::string> commands;
    split_string(commands, cmdBuf, ';');

    std::string model_name        = commands[0];
    std::string share_memory_name = commands[1];
    size_t share_memory_size      = std::stoull(commands[2]);
    std::string strBufferArray    = commands[3];
    std::string perfProfile       = commands[4];
    size_t graphIndex             = std::stoull(commands[5]);
    // commands[6]: "async" or "sync" (optional, default sync)
    bool async_infer = (commands.size() > 6 && commands[6] == "async");

    // If the model was loaded asynchronously, wait for it to finish before inference.
    if (!WaitForAsyncLoad(model_name)) {
        QNN_ERR("ModelRun: async model load failed for '%s'\n", model_name.c_str());
        channel->Write(ACTION_FAILED, strlen(ACTION_FAILED) + 1);
        return;
    }

    if (async_infer) {
        // Generate a unique request_id and reply immediately.
        uint64_t req_id = ++g_request_counter;
        std::string request_id = model_name + "_req_" + std::to_string(req_id);

        // Use a unique SHM registration key per request to avoid aliasing when
        // two async inferences for the same model run concurrently.
        std::string shm_key = share_memory_name + "_" + request_id;

        // On POSIX the fd must survive until the lambda finishes; dup it so the
        // server's main loop closing posixFd doesn't affect the lambda.
#ifndef _WIN32
        ipc::ShmHandle lambdaFd = (posixFd >= 0) ? ::dup(posixFd) : posixFd;
#else
        ipc::ShmHandle lambdaFd = posixFd;
#endif

        std::future<InferResult> fut = std::async(std::launch::async,
            [=]() mutable -> InferResult {
                InferResult result;

                void* lpBase = OpenShareMem(share_memory_name, share_memory_size, lambdaFd, shm_key);
                if (!lpBase) {
#ifndef _WIN32
                    if (lambdaFd >= 0) ::close(lambdaFd);
#endif
                    return result;
                }

                std::vector<uint8_t*> inputBuffers, outputBuffers;
                std::vector<size_t>   inputSize, outputSize;
                // outputSize must start empty — ModelInference populates it.
                ShareMemToVector(strBufferArray, (uint8_t*)lpBase, inputBuffers, inputSize);

                result.success = g_LibAppBuilder.ModelInference(
                    model_name.c_str(), inputBuffers, outputBuffers, outputSize,
                    perfProfile, graphIndex, share_memory_size);

                if (result.success) {
                    auto strResultArray = VectorToShareMem(
                        share_memory_size, (uint8_t*)lpBase, outputBuffers, outputSize);
                    result.offsets = strResultArray.first;
                    result.sizes   = strResultArray.second;
                }
                outputBuffers.clear();
                outputSize.clear();
                CloseShareMem(shm_key);
                return result;
            });

        {
            std::lock_guard<std::mutex> lk(g_infer_results_mutex);
            g_infer_futures[request_id] = std::move(fut);
        }
        // Send request_id back immediately so client can do other work.
        channel->Write(request_id.c_str(), request_id.length() + 1);
    } else {
        // Synchronous path (original behaviour).
        void* lpBase = OpenShareMem(share_memory_name, share_memory_size, posixFd);

        std::vector<uint8_t*> inputBuffers, outputBuffers;
        std::vector<size_t>   inputSize, outputSize;
        // outputSize must start empty — ModelInference populates it.
        ShareMemToVector(strBufferArray, (uint8_t*)lpBase, inputBuffers, inputSize);

        Print_MemInfo("ModelRun::ModelInference Start.");
        BOOL bSuccess = g_LibAppBuilder.ModelInference(
            model_name.c_str(), inputBuffers, outputBuffers, outputSize,
            perfProfile, graphIndex, share_memory_size);
        Print_MemInfo("ModelRun::ModelInference End.");

        std::pair<std::string, std::string> strResultArray =
            VectorToShareMem(share_memory_size, (uint8_t*)lpBase, outputBuffers, outputSize);

        outputBuffers.clear();
        outputSize.clear();
        CloseShareMem(share_memory_name);

        std::string command = strResultArray.first + "=" + strResultArray.second;
        if (bSuccess) {
            channel->Write(command.c_str(), command.length() + 1);
        } else {
            channel->Write(ACTION_FAILED, strlen(ACTION_FAILED) + 1);
        }
    }
}

// Client sends 'q' + request_id to collect the result of an async inference.
void ModelQueryResult(std::string cmdBuf, ipc::IpcChannel* channel) {
    std::string request_id = cmdBuf;   // everything after 'q'

    std::future<InferResult> fut;
    {
        std::lock_guard<std::mutex> lk(g_infer_results_mutex);
        auto it = g_infer_futures.find(request_id);
        if (it != g_infer_futures.end()) {
            fut = std::move(it->second);
            g_infer_futures.erase(it);
        }
    }

    if (!fut.valid()) {
        QNN_ERR("ModelQueryResult: unknown request_id '%s'\n", request_id.c_str());
        channel->Write(ACTION_FAILED, strlen(ACTION_FAILED) + 1);
        return;
    }

    InferResult result = fut.get();   // blocks until background inference finishes

    if (result.success) {
        std::string command = result.offsets + "=" + result.sizes;
        channel->Write(command.c_str(), command.length() + 1);
    } else {
        channel->Write(ACTION_FAILED, strlen(ACTION_FAILED) + 1);
    }
}

void ModelRelease(std::string cmdBuf, ipc::IpcChannel* channel) {
    BOOL bSuccess;
    Print_MemInfo("ModelRelease Start.");

    std::vector<std::string> commands;
    split_string(commands, cmdBuf, ';');

    std::string model_name = commands[0];

    // Ensure any pending async load finishes before destroying the model.
    if (!WaitForAsyncLoad(model_name)) {
        QNN_ERR("ModelRelease: async model load failed for '%s', proceeding with destroy\n", model_name.c_str());
        // Continue with destroy even if load failed, to clean up resources.
    }

    Print_MemInfo("ModelRelease::ModelDestroy Start.");
    QNN_INF("ModelRelease::ModelDestroy %s\n", model_name.c_str());
    bSuccess = g_LibAppBuilder.ModelDestroy(model_name.c_str());
    QNN_INF("ModelRelease::ModelDestroy End ret = %d\n", bSuccess);
    Print_MemInfo("ModelRelease::ModelDestroy End.");

    if (bSuccess) {
        channel->Write(ACTION_OK, strlen(ACTION_OK) + 1);
    }
    else {
        channel->Write(ACTION_FAILED, strlen(ACTION_FAILED) + 1);
    }
}

// Apply / release the HTP perf profile inside this Svc process, where the model
// (and thus the QNN backend handle) actually lives. Called for the 'p' / 'e'
// commands forwarded by the client's PerfProfile.SetPerfProfileGlobal / Rel.
void SetPerf(std::string cmdBuf, ipc::IpcChannel* channel) {
    WaitForAllAsyncLoads();   // backend handle must exist before applying perf profile
    std::string perf_profile = cmdBuf;   // remainder after the 'p' command byte
    BOOL bSuccess = SetPerfProfileGlobal(perf_profile);
    if (bSuccess) {
        channel->Write(ACTION_OK, strlen(ACTION_OK) + 1);
    }
    else {
        channel->Write(ACTION_FAILED, strlen(ACTION_FAILED) + 1);
    }
}

void RelPerf(ipc::IpcChannel* channel) {
    WaitForAllAsyncLoads();   // backend handle must exist before releasing perf profile
    BOOL bSuccess = RelPerfProfileGlobal();
    if (bSuccess) {
        channel->Write(ACTION_OK, strlen(ACTION_OK) + 1);
    }
    else {
        channel->Write(ACTION_FAILED, strlen(ACTION_FAILED) + 1);
    }
}

std::string vec2str(const std::vector<std::string>& vec) {
    std::ostringstream oss;
    for (size_t i = 0; i < vec.size(); i++) {
        if (i > 0) oss << ",";
        oss << vec[i];
    }
    return oss.str();
}

std::string vec2str(const std::vector<std::vector<size_t>>& vec) {
    std::ostringstream oss;
    for (size_t i = 0; i < vec.size(); i++) {
        if (i > 0) oss << "|";   // split with | 
        for (size_t j = 0; j < vec[i].size(); j++) {
            if (j > 0) oss << ",";
            oss << vec[i][j];
        }
    }
    return oss.str();
}

void getModelInfo(std::string cmdBuf, ipc::IpcChannel* channel) {
    BOOL bSuccess = true;
    std::vector<std::string> commands;
    split_string(commands, cmdBuf, ';');
    std::string model_name   = commands[0];
    std::string input        = commands[1];

    // Ensure any pending async load finishes before querying model info.
    if (!WaitForAsyncLoad(model_name)) {
        QNN_ERR("getModelInfo: async model load failed for '%s'\n", model_name.c_str());
        channel->Write(ACTION_FAILED, strlen(ACTION_FAILED) + 1);
        return;
    }

    ModelInfo_t output = g_LibAppBuilder.getModelInfo(model_name, input);
    std::string command;
    std::ostringstream oss;
    oss << vec2str(output.inputShapes)   << ";"
        << vec2str(output.inputDataType) << ";"
        << vec2str(output.outputShapes)  << ";"
        << vec2str(output.outputDataType)<< ";"
        << vec2str(output.inputName)<< ";"
        << vec2str(output.outputName)<< ";"
        << output.graphName << ";";
    command = oss.str();

    //printf("getModelInfo in main.cpp,command=%s\n", command.c_str());
    if (bSuccess) {
        channel->Write(command.c_str(), command.length() + 1);
    }
    else {
        channel->Write(ACTION_FAILED, strlen(ACTION_FAILED) + 1);
    }
}

int svcprocess_run(ipc::IpcChannel* channel) {
    size_t dwRead = 0;
    BOOL bSuccess = false;

    if (!channel) {
        ErrorExit("Svc::Failed to get command channel.");
    }

    for (;;) {
        bSuccess = channel->Read(g_buffer, GLOBAL_BUFSIZE, dwRead);

        if (!bSuccess || dwRead == 0) {
            // The parent closed the command channel (e.g. after destroying the
            // last model in this process). This is the normal shutdown path, not
            // an error: the service loop simply exits and the process ends.
            QNN_INF("Svc::Command channel closed by parent. Service exiting normally.\n");
            break;
        }

        // Any fd that arrived with this message (POSIX SCM_RIGHTS); kInvalidShm() on Windows.
        ipc::ShmHandle posixFd = channel->TakePendingFd();

        char* cmdBuf = g_buffer + 1;
        switch (g_buffer[0]) {
            case 'l':   // load model.
                ModelLoad(cmdBuf, channel);
                break;

            case 'g':   // run Graphs (sync or async).
                ModelRun(cmdBuf, channel, posixFd);
                break;

            case 'q':   // query async inference result.
                ModelQueryResult(cmdBuf, channel);
                break;

            case 'r':   // release model.
                ModelRelease(cmdBuf, channel);
                break;

            case 'i':   // get model info.
                getModelInfo(cmdBuf, channel);
                break;

            case 'p':   // set perf profile (in this process, where the model lives).
                SetPerf(cmdBuf, channel);
                break;

            case 'e':   // release perf profile.
                RelPerf(channel);
                break;
        }
    }

    return 0;
}


// ============================== Client / QAIAppSvc ============================== //
#define BUFSIZE             (256)

// test code, load and run model.
int hostprocess_run(std::string qnn_lib_path, std::string model_path,
                    std::string input_raw_path, int input_count, int memory_size,
                    std::string perf_profile, std::vector <LoraAdapter>& Adapters ) {
    BOOL result = false;

    std::string MODEL_NAME = "<model_name>";
    std::string PROC_NAME = "<proc_name>";

    std::string model_memory_name = MODEL_NAME;
    std::string model_name = MODEL_NAME;
    std::string proc_name = PROC_NAME;

    std::string backend_lib_path = qnn_lib_path + "\\QnnHtp.dll";
    std::string system_lib_path = qnn_lib_path + "\\QnnSystem.dll";

    std::string input_data_path = input_raw_path + "\\input_%d.raw";
    std::string output_data_path = input_raw_path + "\\output_%d.raw";

    QNN_INF("Load data from raw data file to vector Start.\n");
    std::vector<uint8_t*> inputBuffers;
    std::vector<size_t> inputSize;
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;
    char dataPath[BUFSIZE];

    for (int i = 0; i < input_count; i++) {
        sprintf_s(dataPath, BUFSIZE, input_data_path.c_str(), i);
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

    LibAppBuilder libAppBuilder;

    if (0 == memory_size) {    // Load & run model locally.
        QNN_INF("Load and run model locally Start.\n");
        result = libAppBuilder.ModelInitialize(model_name, model_path, backend_lib_path, system_lib_path, Adapters);
        Print_MemInfo("ModelInitialize End.");

        // SetPerfProfileGlobal("burst");

        {
            // Inference.
            result = libAppBuilder.ModelInference(model_name, inputBuffers, outputBuffers, outputSize, perf_profile);

            // Verify the output data here. Free the data in vector.
            for (int i = 0; i < outputSize.size(); i++) {
                sprintf_s(dataPath, BUFSIZE, output_data_path.c_str(), i);
                std::ofstream os(dataPath, std::ofstream::binary);
                if (!os) {
                    QNN_ERR("Failed to open output file for writing: %s", dataPath);
                }
                else {
                    os.write(reinterpret_cast<char*>(&(*(outputBuffers[i]))), outputSize[i]);
                }
                os.close();
            }

            for (int i = 0; i < outputBuffers.size(); i++) {
                free(outputBuffers[i]);
            }
            outputBuffers.clear();
            outputSize.clear();
            Print_MemInfo("ModelInference End.");
        }

        result = libAppBuilder.ModelDestroy(model_name);

        QNN_INF("Load and run model locally End.\n");

        Print_MemInfo("Load and run model locally End.");
    }
    else {    // Load & run model in remote process.
        libAppBuilder.CreateShareMemory(model_memory_name, memory_size);

        // Add 'TalkToSvc_*' function to 'libAppBuilder'.
        QNN_INF("TalkToSvc_Initialize Start.\n");
        result = libAppBuilder.ModelInitialize(model_name, proc_name, model_path, backend_lib_path, system_lib_path);
        QNN_INF("TalkToSvc_Initialize End %d.\n", result);

        QNN_INF("TalkToSvc_Inference Start.\n");
        result = libAppBuilder.ModelInference(model_name, proc_name, model_memory_name, inputBuffers, inputSize, outputBuffers, outputSize, perf_profile);
        QNN_INF("TalkToSvc_Inference End %d.\n", result);

        // Verify the output data here. Free the data in vector.
        for (int i = 0; i < outputSize.size(); i++) {
            sprintf_s(dataPath, BUFSIZE, output_data_path.c_str(), i);
            std::ofstream os(dataPath, std::ofstream::binary);
            if (!os) {
                QNN_ERR("Failed to open output file for writing: %s", dataPath);
            }
            else {
                os.write(reinterpret_cast<char*>(&(*(outputBuffers[i]))), outputSize[i]);
            }
            os.close();
        }

        result = libAppBuilder.ModelDestroy(model_name, proc_name);
        QNN_INF("TalkToSvc_Destroy End.\n");

        libAppBuilder.DeleteShareMemory(model_memory_name);
        QNN_INF("DeleteShareMem End.\n");

        // outputBuffers is in ShareMemory, so we don't need to free this memory.
    }

    // Release input buffer.
    for (int i = 0; i < inputBuffers.size(); i++) {
        free(inputBuffers[i]);
    }
    inputBuffers.clear();
    inputSize.clear();

    return 0;
}


// Function to parse arguments
std::map<std::string, std::vector<std::string>> parse_arguments(int argc, char* argv[]) {
    std::map<std::string, std::vector<std::string>> args;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg.find("--") == 0) { // Check if it's a named argument
            if (i + 1 < argc && argv[i + 1][0] != '-') {
                args[arg].push_back(argv[++i]); // Add to the vector for the corresponding key
            }
            else {
                args[arg].emplace_back(""); // Handle flags or arguments without values
            }
        }
        else {
            throw std::invalid_argument("Invalid argument format: " + arg);
        }
    }
    return args;
}

// Function to parse binary_updates into a map
std::map<std::string, std::vector<std::string>> parse_binary_updates(
    const std::vector<std::string>& binary_update_args) {
    std::map<std::string, std::vector<std::string>> binary_updates;

    for (const auto& update : binary_update_args) {
        std::istringstream ss(update);
        std::string graph_name, path_list;

        // Extract graph name (before the comma)
        if (!std::getline(ss, graph_name, ',')) {
            throw std::invalid_argument("Invalid format for binary update (missing graph name)");
        }

        // Extract the paths (semicolon-separated after the comma)
        if (!std::getline(ss, path_list)) {
            throw std::invalid_argument("Invalid format for binary update (missing paths)");
        }

        // Split the paths by ';'
        std::vector<std::string> paths;
        std::istringstream path_stream(path_list);
        std::string path;
        while (std::getline(path_stream, path, ';')) {
            paths.push_back(path);
        }

        if (paths.empty()) {
            throw std::invalid_argument("Invalid format for binary update (missing paths)");
        }

        binary_updates[graph_name] = paths;
    }

    return binary_updates;
}




int main(int argc, char** argv) {
    if (argc > 1 && argv[1] && argv[1][0] == 's') {  // Start server.
        uint64_t inRead   = std::stoull(argv[2]);
        uint64_t outWrite = std::stoull(argv[3]);
        SetLogLevel(std::stoi(argv[5]));
        SetProfilingLevel(std::stoi(argv[6]));
        SetProcInfo(argv[7], std::stoull(argv[4]));
        QNN_INF("Svc App Start proc %s.\n", argv[7]);
        Print_MemInfo("Svc App Start.");

        auto channel = ipc::IpcChannel::AttachChild(inRead, outWrite);
        svcprocess_run(channel.get());

        Print_MemInfo("Svc App End.");
    }
    else {  // Start test mode to load & run model.
        /* Command formant: QAIAppSvc.exe --log_level <int:log_level> --QNN_Libraries_Path <str:QNN_Libraries_Path> 
                                          --model_path <str:model_path> --perf_profile <str:perf_profile> --input_path <str:input_raw_path> 
                                          --input_count <int:input_count> --memory_size<int:memory_size> 
                                          --binary_updates<str:graph_name,binary_update_path_1;binary_update_path_2>
         input files are under 'input_raw_path' and the file names format are 'input_%d.raw'. 
         */

        try {
            // Parse command-line arguments
            auto args = parse_arguments(argc, argv);

            // Extract and validate required parameters
            int log_level = std::stoi(args["--log_level"][0]);
            std::string qnn_lib_path = args["--QNN_Libraries_Path"][0];
            std::string model_path = args["--model_path"][0];
            std::string perf_profile = args["--perf_profile"][0];
            std::string input_list_path = args["--input_path"][0];
            int input_count = std::stoi(args["--input_count"][0]);

            // Handle optional parameters
            int memory_size = 0;
            if (args.count("--memory_size")) {
                memory_size = std::stoi(args["--memory_size"][0]);
            }

            std::map<std::string, std::vector<std::string>> binary_updates;
            if (args.count("--binary_updates")) {
                binary_updates = parse_binary_updates(args["--binary_updates"]);
            }

            SetLogLevel(log_level);

            if (log_level >= 5) {
                SetProfilingLevel(2);
            }
            else if (log_level >= 3) {
                SetProfilingLevel(1);
            }

            // Creating list of adapters 
            std::vector<LoraAdapter> Adapters;
            for (const auto& update : binary_updates) {
                std::string graph_name = update.first;
                std::vector<std::string> bin_path = update.second;
                LoraAdapter Adapter(graph_name, bin_path);
                Adapters.push_back(Adapter);
            }
            

            hostprocess_run(qnn_lib_path, model_path, input_list_path, input_count, memory_size, perf_profile, Adapters);

        }
        catch (const std::exception& e) {
            std::cerr << "Error: " << e.what() << "\n";

            printf("Command formant: QAIAppSvc.exe --log_level <int:log_level> --QNN_Libraries_Path <str:QNN_Libraries_Path> --model_path <str:model_path> --perf_profile <str:perf_profile> --input_path <str:input_raw_path> --input_count <int:input_count> --memory_size<int:memory_size> --binary_updates<str:graph_name,binary_update_path_1;binary_update_path_2>\n");
            printf("'memory_size' is an option parameter, only needed while running the model in remote process.\n");
            printf("--binary_updates is an optional parameter that can be passed if you want to apply adapters to the graph. This parameter can be specified multiple times if needed.\n");
            printf("Example: --log_level 2 --QNN_Libraries_Path C:\\user\\lorav2\\qnn_assets\\2.28.2 --model_path C:\\user\\lorav2\\running_sample_app\\models_and_input\\text_encoder.serialized_qnn_2.28.bin --perf_profile burst --input_path C:\\user\\lorav2\\runnig_qai_helper\\text_encoder_inputs --input_count 2 --binary_updates text_encoder,C:\\user\\lorav2\\running_sample_app\\models_and_input\\text_encoder_Stickers_qnn_2.28.bin;C:\\user\\lorav2\\running_sample_app\\models_and_input\\text_encoder_TShirtDesignAF.bin  --memory_size 102400000\n");
            return 1;
        }

        Print_MemInfo("Main App End.");
    }

    return 0;
}

