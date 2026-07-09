//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#ifndef _LIBAPPBUILDER_UTILS_H
#define _LIBAPPBUILDER_UTILS_H


#include <sstream>
#include <vector>
#include <string>
#include <limits>
#include <memory>
#include <cstring>
#include <cerrno>

#ifdef _WIN32
#include <windows.h>
#endif

#include "Utils/ShareMem.hpp"
#include "ipc/IpcChannel.hpp"
#include "ipc/SvcProcess.hpp"

#define GLOBAL_BUFSIZE      4096

// Executable name used to spawn the service process. On both platforms this is
// resolved via PATH search (CreateProcess / posix_spawnp); the Python wrapper
// puts the qai_appbuilder package directory on PATH so the service binary that
// ships next to libappbuilder is found.
#ifdef _WIN32
#define SVC_APPBUILDER_EXE  "QAIAppSvc.exe"
#else
#define SVC_APPBUILDER_EXE  "QAIAppSvc"
#endif

uint64_t g_logEpoch = 0;
int g_logLevel = 0;
int g_profilingLevel = 0;
std::string g_ProcName = "^main";

char g_buffer[GLOBAL_BUFSIZE];

// Per-service-process state: the command channel and the child process handle.
// Declaration order matters: 'channel' is declared after 'process' so that on
// destruction the channel (socket/pipe) is closed FIRST. Closing it gives the
// child an EOF on its next read, which makes its loop exit; only then is
// 'process' destroyed (and the child reaped).
typedef struct ProcInfo {
    std::unique_ptr<ipc::SvcProcess> process;
    std::unique_ptr<ipc::IpcChannel> channel;
} ProcInfo_t;

std::unordered_map<std::string, std::unique_ptr<ProcInfo_t>> sg_proc_info_map;   // proc_name -> ProcInfo_t
std::unordered_map<std::string, ProcInfo_t*> sg_model_info_map;                  // model_name -> ProcInfo_t

#ifdef _WIN32
std::string GetLastErrorAsString(std::string message) {
    DWORD errorMessageID = ::GetLastError();
    if (errorMessageID == 0)
        return std::string();

    LPSTR messageBuffer = nullptr;
    size_t size = FormatMessageA(FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
        NULL, errorMessageID, MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT), (LPSTR)&messageBuffer, 0, NULL);
    std::string result(messageBuffer, size);
    LocalFree(messageBuffer);

    return message + " Error: [" + std::to_string(errorMessageID) + "] " + result;
}
#else
std::string GetLastErrorAsString(std::string message) {
    int e = errno;
    if (e == 0) return std::string();
    return message + " Error: [" + std::to_string(e) + "] " + std::string(std::strerror(e));
}
#endif

void ErrorExit(std::string message) {
    QNN_ERR(GetLastErrorAsString(message).c_str());
    exit(1);
}

void split_string(std::vector<std::string> & output, const std::string &input, const char separator) {
  std::istringstream tokenStream(input);
  while (!tokenStream.eof()) {
    std::string value;
    getline(tokenStream, value, separator);
    if (!value.empty()) {
        output.push_back(value);
    }
  }
}

ProcInfo_t* FindProcInfo(std::string proc_name) {
    auto it = sg_proc_info_map.find(proc_name);
    if (it != sg_proc_info_map.end()) {
        if (it->second) {
            return it->second.get();
        }
    }

    return nullptr;
}

ProcInfo_t* CreateSvcProcess(std::string proc_name) {
    ipc::ChildEndpoints ends;
    auto channel = ipc::IpcChannel::CreateParent(ends);
    if (!channel) {
        ErrorExit("Create command channel failed.");
        return nullptr;
    }

    auto process = ipc::SvcProcess::Spawn(SVC_APPBUILDER_EXE, proc_name, ends,
                                          g_logEpoch, g_logLevel, g_profilingLevel);
    if (!process) {
        ErrorExit("Spawn service process failed.");
        return nullptr;
    }

    // The child has inherited its endpoints; the parent no longer needs them.
    channel->CloseInheritedEnds();

    auto pProcInfo = std::make_unique<ProcInfo_t>();
    pProcInfo->channel = std::move(channel);
    pProcInfo->process = std::move(process);

    ProcInfo_t* raw = pProcInfo.get();
    sg_proc_info_map[proc_name] = std::move(pProcInfo);

    QNN_INF("CreateSvcProcess Success!");
    return raw;
}

BOOL StopSvcProcess(std::string proc_name) {
    auto it = sg_proc_info_map.find(proc_name);
    if (it == sg_proc_info_map.end()) {
        QNN_ERR("StopSvcProcess::Cant find this process %s.\n", proc_name.c_str());
        return false;
    }

    // Destroying ProcInfo_t closes the channel (signals the child to exit) and
    // reaps the process.
    sg_proc_info_map.erase(it);
    return true;
}

// Send model data to the Svc through the command channel and receive the response.
BOOL TalkToSvc_Initialize(const std::string& model_name, const std::string& proc_name, const std::string& model_path,
                          const std::string& backend_lib_path, const std::string& system_lib_path, bool async, const std::string& input_data_type, const std::string& output_data_type) {
    ProcInfo_t* pProcInfo = FindProcInfo(proc_name);
    if (!pProcInfo) {
        pProcInfo = CreateSvcProcess(proc_name);

        if (!pProcInfo) return false;
    }

    ipc::IpcChannel* channel = pProcInfo->channel.get();
    size_t dwRead = 0;
    bool bSuccess;
    std::string async_str = "sync";

    if (async) {
        async_str = "async";
    }

    std::string command = "l" + model_name + ";" + model_path + ";" + backend_lib_path + ";" + system_lib_path + ";" + async_str + ";" + input_data_type + ";" + output_data_type;

    TimerHelper timerHelper;
    // Write command to Svc.
    bSuccess = channel->Write(command.c_str(), command.length() + 1);
    if (!bSuccess) return false;

    if (!async) {  // We only wait for Svc response when sync mode. Otherwise, we just return.
        // Read command from Svc.
        bSuccess = channel->Read(g_buffer, GLOBAL_BUFSIZE, dwRead);
        if(dwRead) {
            g_buffer[dwRead] = 0;
            QNN_INF("TalkToSvc_Initialize::ReadFromPipe: %s dwRead = %d\n", g_buffer, (int)dwRead);
        }
        else {
            QNN_ERR("TalkToSvc_Initialize::ReadFromPipe: Failed to read from channel, perhaps child process died.\n");
        }
        if (!bSuccess || dwRead == 0) return false;
    }

    timerHelper.Print("TalkToSvc_Initialize::Pipe talk");

    // Add "model_name" to "sg_model_info_map".
    sg_model_info_map.insert(std::make_pair(model_name, pProcInfo));

    return bSuccess;
}

// Forward a perf-profile command to a single Svc process and wait for its ack.
// 'action' is 'p' (set perf profile, payload = perf_profile string) or
// 'e' (release perf profile, no payload).
static BOOL TalkToSvc_PerfOne(ProcInfo_t* pProcInfo, char action, const std::string& perf_profile) {
    ipc::IpcChannel* channel = pProcInfo->channel.get();
    size_t dwRead = 0;
    bool bSuccess;

    std::string command = std::string(1, action) + perf_profile;
    bSuccess = channel->Write(command.c_str(), command.length() + 1);
    if (!bSuccess) return false;

    bSuccess = channel->Read(g_buffer, GLOBAL_BUFSIZE, dwRead);
    if (!bSuccess || dwRead == 0) {
        QNN_ERR("TalkToSvc_Perf::Failed to read from channel, perhaps child process died.\n");
        return false;
    }
    g_buffer[dwRead] = 0;
    return (g_buffer[0] != 'F');   // 'F' == ACTION_FAILED
}

// The Python PerfProfile.SetPerfProfileGlobal / RelPerfProfileGlobal are process
// global and carry no proc_name. In cross-process mode the models live in the
// Svc child processes, so broadcast the perf command to every Svc process. With
// no Svc process running (pure in-process mode) this returns false so the caller
// falls back to applying the perf profile locally.
BOOL TalkToSvc_SetPerfProfileGlobal(const std::string& perf_profile) {
    if (sg_proc_info_map.empty()) return false;
    BOOL ok = true;
    for (auto& kv : sg_proc_info_map) {
        if (kv.second) {
            ok = TalkToSvc_PerfOne(kv.second.get(), 'p', perf_profile) && ok;
        }
    }
    return ok;
}

BOOL TalkToSvc_RelPerfProfileGlobal() {
    if (sg_proc_info_map.empty()) return false;
    BOOL ok = true;
    for (auto& kv : sg_proc_info_map) {
        if (kv.second) {
            ok = TalkToSvc_PerfOne(kv.second.get(), 'e', "") && ok;
        }
    }
    return ok;
}

BOOL TalkToSvc_Destroy(std::string model_name, std::string proc_name) {
    ProcInfo_t* pProcInfo = FindProcInfo(proc_name);
    if (!pProcInfo) {
        QNN_ERR("TalkToSvc_Destroy::Cant find this process %s.\n", proc_name.c_str());
        return false;
    }

    ipc::IpcChannel* channel = pProcInfo->channel.get();
    size_t dwRead = 0;
    bool bSuccess;

    std::string command = "r" + model_name;

    TimerHelper timerHelper;
    // Write command to Svc.
    bSuccess = channel->Write(command.c_str(), command.length() + 1);
    QNN_INF("TalkToSvc_Destroy::WriteToPipe: %s\n", command.c_str());
    if (!bSuccess) return false;

    // Read command from Svc.
    bSuccess = channel->Read(g_buffer, GLOBAL_BUFSIZE, dwRead);
    if (dwRead) {
        g_buffer[dwRead] = 0;
        QNN_INF("TalkToSvc_Destroy::ReadFromPipe: %s dwRead = %d\n", g_buffer, (int)dwRead);
    }
    else {
        QNN_ERR("TalkToSvc_Destroy::ReadFromPipe: Failed to read from channel, perhaps child process died.\n");
    }
    if (!bSuccess || dwRead == 0) return false;
    timerHelper.Print("TalkToSvc_Destroy::Pipe talk");

    sg_model_info_map.erase(model_name);
    if(sg_model_info_map.size() == 0) {     // If no model in this process, stop this process.
        QNN_INF("TalkToSvc_Destroy::StopSvcProcess.\n");
        StopSvcProcess(proc_name);
    }

    return bSuccess;
}

// The format of strStringSize: "124,3333,434343,132", included the inputSize content.
void ShareMemToVector(std::string strBufferArray, uint8_t* lpBase, std::vector<uint8_t*>& buffers, std::vector<size_t>& size) {
    std::vector<std::string> strArray;
    std::vector<std::string> strOffsetArray;
    std::vector<std::string> strSizeArray;
    split_string(strArray, strBufferArray, '=');
    split_string(strOffsetArray, strArray[0], ',');
    split_string(strSizeArray, strArray[1], ',');

    size_t offset = 0;
    size_t dataSize = 0;

    // Perhaps the data in buffer is not in order.
    for (int i = 0; i < strOffsetArray.size(); i++) {
        offset = std::stoull(strOffsetArray[i]);
        dataSize = std::stoull(strSizeArray[i]);
        size.push_back(dataSize);
        buffers.push_back(reinterpret_cast<uint8_t*>(lpBase + offset));
    }
}

// Copy data to 'pShareMemInfo->lpBase'. If the data in 'buffers' has been in the area of share memory, don't copy.
std::pair<std::string, std::string> VectorToShareMem(size_t share_memory_size, uint8_t* lpBase, std::vector<uint8_t*>& buffers, std::vector<size_t>& size) {
    QNN_INF("VectorToShareMem Start. size = %llu\n", share_memory_size);
    //TimerHelper timerHelper;

    std::string strOffsetArray = "";
    std::string strSizeArray = "";
    size_t offset = 0;
    size_t dataSize = 0;
    uint8_t* buffer = nullptr;

    // How to handle the case - part of the data in buffers are in the share memory?
    // Calculate the offset for out-of-shm copies: must start AFTER the last byte
    // of any buffer that already lives in shm, so we don't overwrite it.
    for (int i = 0; i < (int)buffers.size(); i++) {
        buffer = buffers[i];
        if (buffer >= lpBase && buffer < lpBase + share_memory_size) {     // This buffer is in the share memory area.
            size_t end = (size_t)(buffer - lpBase) + size[i];
            if (end > offset) offset = end;
        }
    }

    // Copy the data which is not in share memory to share memory.
    for (int i = 0; i < buffers.size(); i++) {
        buffer = buffers[i];
        dataSize = size[i];
        if (buffer >= lpBase && buffer < lpBase + share_memory_size) {     // This buffer is in the share memory area.
            strOffsetArray += std::to_string(buffer - lpBase) + ",";
            //QNN_INF("VectorToShareMem in buffers, ignore copy.\n");
        }
        else {
            memcpy((uint8_t*)lpBase + offset, buffers[i], dataSize);        // This buffer is NOT in the share memory area, copy it.
            strOffsetArray += std::to_string(offset) + ",";
            offset += dataSize;
            //QNN_INF("VectorToShareMem NOT in buffers, copy...\n");
        }
        strSizeArray += std::to_string(dataSize) + ",";
    }

    //timerHelper.Print("VectorToShareMem::offset = " + std::to_string(offset));
    // QNN_INF("VectorToShareMem End.\n");
    // QNN_INF("VectorToShareMem::strOffsetArray = %s.\n", strOffsetArray.c_str());
    return std::make_pair(strOffsetArray, strSizeArray);
}

// Send model data to the Svc through share memory and receive model generated data from share memory.
BOOL TalkToSvc_Inference(std::string model_name, std::string proc_name, std::string share_memory_name,
                         std::vector<uint8_t*>& inputBuffers, std::vector<size_t>& inputSize,
                         std::vector<uint8_t*>& outputBuffers, std::vector<size_t>& outputSize,
                         std::string perfProfile, size_t graphIndex) {
    ProcInfo_t* pProcInfo = FindProcInfo(proc_name);
    if (!pProcInfo) {
        QNN_ERR("TalkToSvc_Inference::Cant find this process %s.\n", proc_name.c_str());
        return false;
    }

    ShareMemInfo_t* pShareMemInfo = FindShareMem(share_memory_name);
    if (!pShareMemInfo) {
        QNN_ERR("TalkToSvc_Inference::Cant find this share memory %s.\n", share_memory_name.c_str());
        return false;
    }


    // Early validation to avoid VectorToShareMem memcpy crash.
    if (inputBuffers.size() != inputSize.size()) {
        QNN_ERR("TalkToSvc_Inference: inputBuffers/inputSize length mismatch. buffers=%zu size=%zu\n", inputBuffers.size(), inputSize.size());
        return false;
    }
    if (!pShareMemInfo->lpBase || pShareMemInfo->size == 0) {
        QNN_ERR("TalkToSvc_Inference: invalid share memory base or size. name=%s lpBase=%p size=%llu\n", share_memory_name.c_str(), pShareMemInfo->lpBase, (unsigned long long)pShareMemInfo->size);
        return false;
    }

    // Compute required size according to VectorToShareMem's offset strategy: reserve sizes of in-share buffers + sizes of out-of-share buffers.
    {
        uint8_t* base = (uint8_t*)pShareMemInfo->lpBase;
        uint8_t* end = base + pShareMemInfo->size;
        size_t reserved = 0;
        size_t toCopy = 0;

        for (size_t i = 0; i < inputBuffers.size(); ++i) {
            uint8_t* buf = inputBuffers[i];
            size_t sz = inputSize[i];

            if (!buf && sz > 0) {
                QNN_ERR("TalkToSvc_Inference: null input buffer at index %zu with non-zero size %llu\n", i, (unsigned long long)sz);
                return false;
            }

            // In-share: [base, end)
            if (buf >= base && buf < end) {
                if (sz > 0 && ((size_t)(end - buf) < sz)) {
                    QNN_ERR("TalkToSvc_Inference: in-share input buffer out of bounds. idx=%zu buf=%p size=%llu share=[%p,%p)\n", i, buf, (unsigned long long)sz, base, end);
                    return false;
                }
                if (std::numeric_limits<size_t>::max() - reserved < sz) {
                    QNN_ERR("TalkToSvc_Inference: size_t overflow while accumulating reserved. idx=%zu\n", i);
                    return false;
                }
                reserved += sz;
            } else {
                if (std::numeric_limits<size_t>::max() - toCopy < sz) {
                    QNN_ERR("TalkToSvc_Inference: size_t overflow while accumulating toCopy. idx=%zu\n", i);
                    return false;
                }
                toCopy += sz;
            }
        }

        if (std::numeric_limits<size_t>::max() - reserved < toCopy) {
            QNN_ERR("TalkToSvc_Inference: size_t overflow while computing totalNeeded.\n");
            return false;
        }

        size_t totalNeeded = reserved + toCopy;
        if (totalNeeded > pShareMemInfo->size) {
            QNN_ERR("TalkToSvc_Inference: share memory too small. required=%llu (reserved=%llu copy=%llu) share_size=%llu name=%s\n",
                    (unsigned long long)totalNeeded, (unsigned long long)reserved, (unsigned long long)toCopy, (unsigned long long)pShareMemInfo->size, share_memory_name.c_str());
            return false;
        }
    }


    ipc::IpcChannel* channel = pProcInfo->channel.get();
    size_t dwRead = 0;
    bool bSuccess;

    std::string command = "g" + model_name + ";" + share_memory_name + ";" + std::to_string(pShareMemInfo->size) + ";";
    // 'offset' in share memory(according to 'inputBuffers' data size, so that we can restore this data to 'std::vector<uint8_t*>' in Svc).
    std::pair<std::string, std::string> strResultArray = VectorToShareMem(pShareMemInfo->size, (uint8_t*)pShareMemInfo->lpBase, inputBuffers, inputSize);
    command = command + strResultArray.first + "=" + strResultArray.second + ";";
    command = command + perfProfile + ";";
    command = command + std::to_string(graphIndex);

    // start_time();
    // Write command to Svc. On POSIX, the shared-memory fd travels with the
    // command so the server can map it (it cannot be opened by name). On
    // Windows the fd is ignored and the server opens the mapping by name.
    ipc::ShmHandle shmHandle = pShareMemInfo->region ? pShareMemInfo->region->Handle() : ipc::kInvalidShm();
    bSuccess = channel->WriteWithFd(command.c_str(), command.length() + 1, shmHandle);
    if (!bSuccess) return false;

    // Read command from Svc.
    bSuccess = channel->Read(g_buffer, GLOBAL_BUFSIZE, dwRead);
    if(dwRead) {
        g_buffer[dwRead] = 0;
        QNN_INF("TalkToSvc_Inference::ReadFromPipe: %s dwRead = %d\n", g_buffer, (int)dwRead);
    }
    else {
        QNN_ERR("TalkToSvc_Inference::ReadFromPipe: Failed to read from channel, perhaps child process died.\n");
    }
    if (!bSuccess || dwRead == 0) return false;
    //print_time("TalkToSvc_Inference::Pipe talk");

    // Read the output data from 'share_memory_name'.
    if (dwRead) {
        if (g_buffer[0] == 'F') {  // ACTION_FAILED == Failed.
            return false;
        }

        ShareMemToVector(g_buffer, (uint8_t*)pShareMemInfo->lpBase, outputBuffers, outputSize);
    }

    return bSuccess;
}

// Launch inference asynchronously on the Svc side.
// Returns a request_id string; the caller must later call TalkToSvc_WaitInference
// with that id to retrieve the result.
std::string TalkToSvc_InferenceAsync(std::string model_name, std::string proc_name,
                                     std::string share_memory_name,
                                     std::vector<uint8_t*>& inputBuffers,
                                     std::vector<size_t>& inputSize,
                                     std::string perfProfile, size_t graphIndex) {
    ProcInfo_t* pProcInfo = FindProcInfo(proc_name);
    if (!pProcInfo) {
        QNN_ERR("TalkToSvc_InferenceAsync::Cant find this process %s.\n", proc_name.c_str());
        return "";
    }

    ShareMemInfo_t* pShareMemInfo = FindShareMem(share_memory_name);
    if (!pShareMemInfo) {
        QNN_ERR("TalkToSvc_InferenceAsync::Cant find this share memory %s.\n", share_memory_name.c_str());
        return "";
    }

    ipc::IpcChannel* channel = pProcInfo->channel.get();
    size_t dwRead = 0;
    bool bSuccess;

    std::string command = "g" + model_name + ";" + share_memory_name + ";" +
                          std::to_string(pShareMemInfo->size) + ";";
    std::pair<std::string, std::string> strResultArray =
        VectorToShareMem(pShareMemInfo->size, (uint8_t*)pShareMemInfo->lpBase, inputBuffers, inputSize);
    command = command + strResultArray.first + "=" + strResultArray.second + ";";
    command = command + perfProfile + ";";
    command = command + std::to_string(graphIndex) + ";async";   // mark as async

    ipc::ShmHandle shmHandle = pShareMemInfo->region ? pShareMemInfo->region->Handle() : ipc::kInvalidShm();
    bSuccess = channel->WriteWithFd(command.c_str(), command.length() + 1, shmHandle);
    if (!bSuccess) return "";

    // Server replies with request_id immediately (not the full result).
    bSuccess = channel->Read(g_buffer, GLOBAL_BUFSIZE, dwRead);
    if (!bSuccess || dwRead == 0) {
        QNN_ERR("TalkToSvc_InferenceAsync::Failed to read request_id.\n");
        return "";
    }
    g_buffer[dwRead] = 0;
    if (g_buffer[0] == 'F') return "";

    return std::string(g_buffer);
}

// Block until a previously launched async inference (request_id) is complete
// and collect the output buffers from shared memory.
BOOL TalkToSvc_WaitInference(const std::string& request_id,
                              const std::string& proc_name,
                              const std::string& share_memory_name,
                              std::vector<uint8_t*>& outputBuffers,
                              std::vector<size_t>& outputSize) {
    if (request_id.empty()) return false;

    ProcInfo_t* pProcInfo = FindProcInfo(proc_name);
    if (!pProcInfo) {
        QNN_ERR("TalkToSvc_WaitInference::Cant find this process %s.\n", proc_name.c_str());
        return false;
    }

    ShareMemInfo_t* pShareMemInfo = FindShareMem(share_memory_name);
    if (!pShareMemInfo) {
        QNN_ERR("TalkToSvc_WaitInference::Cant find this share memory %s.\n", share_memory_name.c_str());
        return false;
    }

    ipc::IpcChannel* channel = pProcInfo->channel.get();
    size_t dwRead = 0;
    bool bSuccess;

    std::string command = "q" + request_id;
    bSuccess = channel->Write(command.c_str(), command.length() + 1);
    if (!bSuccess) return false;

    bSuccess = channel->Read(g_buffer, GLOBAL_BUFSIZE, dwRead);
    if (dwRead) {
        g_buffer[dwRead] = 0;
        QNN_INF("TalkToSvc_WaitInference::ReadFromPipe: %s dwRead = %d\n", g_buffer, (int)dwRead);
    } else {
        QNN_ERR("TalkToSvc_WaitInference::ReadFromPipe: Failed, perhaps child process died.\n");
    }
    if (!bSuccess || dwRead == 0) return false;
    if (g_buffer[0] == 'F') return false;

    ShareMemToVector(g_buffer, (uint8_t*)pShareMemInfo->lpBase, outputBuffers, outputSize);
    return true;
}

std::vector<std::string> split(const std::string& s, char delim) {
    std::vector<std::string> elems;
    std::stringstream ss(s);
    std::string item;
    while (std::getline(ss, item, delim)) {
        elems.push_back(item);
    }
    return elems;
}

std::vector<std::string> parseStringVector(const std::string& s) {
    if (s.empty()) return {};
    return split(s, ',');
}

std::vector<std::vector<size_t>> parseShapeVector(const std::string& s) {
    std::vector<std::vector<size_t>> result;
    if (s.empty()) return result;
    auto shapes = split(s, '|');
    for (auto& shapeStr : shapes) {
        std::vector<size_t> dims;
        auto dimsStr = split(shapeStr, ',');
        for (auto& d : dimsStr) {
            dims.push_back(std::stoull(d));
        }
        result.push_back(dims);
    }
    return result;
}

ModelInfo_t TalkToSvc_getModelInfo(std::string model_name, std::string proc_name, std::string input) {
    ModelInfo_t output;
    ProcInfo_t* pProcInfo = FindProcInfo(proc_name);
    if (!pProcInfo) {
        return output;
    }

    ipc::IpcChannel* channel = pProcInfo->channel.get();
    size_t dwRead = 0;
    bool bSuccess;

    std::string command = "i" + model_name + ";" + input + ";";

    // Write command to Svc.
    bSuccess = channel->Write(command.c_str(), command.length() + 1);
    if (!bSuccess) return output;

    // Read command from Svc.
    bSuccess = channel->Read(g_buffer, GLOBAL_BUFSIZE, dwRead);
    if(dwRead) {
        g_buffer[dwRead] = 0;
    }
    else {
        printf("TalkToSvc_getModelInfo::ReadFromPipe: Failed to read from channel, perhaps child process died.\n");
    }
    if (!bSuccess || dwRead == 0) return output;

    if (dwRead) {
        if (g_buffer[0] == 'F') {  // ACTION_FAILED == Failed.
            return output;
        }
    }

    auto parts = split(g_buffer, ';');
    if (parts.size() < 7) { //total parts of struct ModelInfo_t
        throw std::runtime_error("Invalid command format: expected 7 parts");
    }

    output.inputShapes    = parseShapeVector(parts[0]);
    output.inputDataType  = parseStringVector(parts[1]);
    output.outputShapes   = parseShapeVector(parts[2]);
    output.outputDataType = parseStringVector(parts[3]);
    output.inputName      = parseStringVector(parts[4]);
    output.outputName     = parseStringVector(parts[5]);
    output.graphName      = parts[6];

    return output;
}
#endif
