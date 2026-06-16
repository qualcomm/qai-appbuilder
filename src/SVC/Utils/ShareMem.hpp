//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#include <iostream>
#include <fstream>
#include <unordered_map>
#include <memory>

#ifdef _WIN32
#include <windows.h>
#include <psapi.h>
#include <direct.h>
#include <process.h>
#include <winbase.h>
#endif

#include "LibAppBuilder.hpp"
#include "ipc/SharedRegion.hpp"


#define PRINT_MEMINFO (0)

// Cross-platform shared-memory descriptor. 'lpBase' and 'size' keep their
// original names so the data-marshalling code in Utils.hpp / main.cpp is
// unchanged; 'region' owns the underlying OS mapping (file mapping on Windows,
// mmap'd fd on POSIX).
typedef struct ShareMemInfo {
    size_t size;
    void*  lpBase;
    std::unique_ptr<ipc::SharedRegion> region;
} ShareMemInfo_t;

std::unordered_map<std::string, std::unique_ptr<ShareMemInfo_t>> sg_share_mem_map;

BOOL Print_MemInfo(std::string TAG) {
#if PRINT_MEMINFO
#ifdef _WIN32
    uint64_t phyUsed = 0, memUsed = 0, pagefileUsed = 0;
    PROCESS_MEMORY_COUNTERS_EX pmc;
    DWORD processID = GetCurrentProcessId();
    HANDLE processHandle = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, FALSE, processID);
    if (GetProcessMemoryInfo(processHandle, (PROCESS_MEMORY_COUNTERS*)&pmc, sizeof(pmc))) {
        phyUsed = pmc.WorkingSetSize / 1024 / 1024;
        memUsed = pmc.PrivateUsage / 1024 / 1024;
        pagefileUsed = pmc.PagefileUsage / 1024 / 1024;
    }
    CloseHandle(processHandle);
    QNN_WAR("[MemInfo][%s]:: phy used: %llu M, mem used: %llu M, pagefile used %llu M", TAG.c_str(), phyUsed, memUsed, pagefileUsed);
#endif
#endif
    return true;
}

ShareMemInfo_t* FindShareMem(std::string share_memory_name) {
    auto it = sg_share_mem_map.find(share_memory_name);
    if (it != sg_share_mem_map.end()) {
        if (it->second) {
            return it->second.get();
        }
    }

    QNN_ERR("FindShareMem::find failed.\n");
    return nullptr;
}

// Register an already-mapped region under a name. Takes ownership of 'region'.
// Returns the stored descriptor, or nullptr on failure.
ShareMemInfo_t* RegisterShareMem(const std::string& share_memory_name,
                                 std::unique_ptr<ipc::SharedRegion> region) {
    if (!region || !region->Base()) {
        return nullptr;
    }

    auto info = std::make_unique<ShareMemInfo_t>();
    info->size   = region->Size();
    info->lpBase = region->Base();
    info->region = std::move(region);

    ShareMemInfo_t* raw = info.get();
    sg_share_mem_map[share_memory_name] = std::move(info);
    return raw;
}

// Client side: create a named shared-memory region.
BOOL CreateShareMem(std::string share_memory_name, size_t share_memory_size) {
    auto region = ipc::SharedRegion::Create(share_memory_name, share_memory_size);
    if (!region) {
        QNN_ERR("CreateShareMem::create failed.\n");
        return false;
    }

    if (!RegisterShareMem(share_memory_name, std::move(region))) {
        QNN_ERR("CreateShareMem::register failed.\n");
        return false;
    }

    QNN_INF("CreateShareMem::Count = %d\n", (int)sg_share_mem_map.size());
    return true;
}

BOOL DeleteShareMem(std::string share_memory_name) {
    auto it = sg_share_mem_map.find(share_memory_name);
    if (it == sg_share_mem_map.end()) {
        QNN_ERR("DeleteShareMem::Cant find this share memory %s.\n", share_memory_name.c_str());
        return false;
    }

    sg_share_mem_map.erase(it);   // unique_ptr dtor unmaps / closes the region
    QNN_INF("DeleteShareMem::Count = %d\n", (int)sg_share_mem_map.size());
    return true;
}
