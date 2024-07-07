//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#include <windows.h>
#include <psapi.h>
#include <iostream>
#include <fstream>
#include <direct.h>
#include <process.h>
#include <winbase.h>
#include <unordered_map>

#include "LibAppBuilder.hpp"


#define PRINT_MEMINFO (0)

typedef struct ShareMemInfo {
    size_t size;
    HANDLE hCreateMapFile;
    LPVOID lpBase;
} ShareMemInfo_t;

std::unordered_map<std::string, ShareMemInfo_t*> sg_share_mem_map;

BOOL Print_MemInfo(std::string TAG) {
#if PRINT_MEMINFO
    /*
    MEMORYSTATUSEX memInfo;
    memInfo.dwLength = sizeof(MEMORYSTATUSEX);
    if (!GlobalMemoryStatusEx(&memInfo)) {
        QNN_ERR("GlobalMemoryStatusEx failed.");
        return false;
    }

    // std::cout << "Shared memory used by this process: " << memInfo.ullAvailPageFile << " bytes" << std::endl;
    QNN_INF("MemInfo:: %d M", memInfo.ullAvailPageFile / 1024 / 1024);
    */

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
    return true;
}

ShareMemInfo_t* FindShareMem(std::string share_memory_name) {
    auto it = sg_share_mem_map.find(share_memory_name);
    if (it != sg_share_mem_map.end()) {
        if (it->second) {
            auto pShareMemInfo = it->second;
            return pShareMemInfo;
        }
    }

    QNN_ERR("FindShareMem::find failed.\n");
    return nullptr;
}

BOOL CreateShareMem(std::string share_memory_name, size_t share_memory_size) {
    DWORD size = (DWORD)share_memory_size;
    HANDLE hCreateMapFile = nullptr;
    LPVOID lpBase = nullptr;
    ShareMemInfo_t* pShareMemInfo = nullptr;

    hCreateMapFile = CreateFileMappingA(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE, 0, 
                                        size, share_memory_name.c_str());

    if(hCreateMapFile) {
        lpBase = MapViewOfFile(hCreateMapFile, FILE_MAP_ALL_ACCESS, 0, 0, size);
    }

    if (lpBase) {
        pShareMemInfo = (ShareMemInfo_t*)malloc(sizeof(ShareMemInfo_t));
        if (pShareMemInfo) {
            pShareMemInfo->size = share_memory_size;
            pShareMemInfo->hCreateMapFile = hCreateMapFile;
            pShareMemInfo->lpBase = lpBase;

            sg_share_mem_map.insert(std::make_pair(share_memory_name, pShareMemInfo));
            QNN_INF("CreateShareMem::Count = %d\n", (int)sg_share_mem_map.size());
            return true;
        }
    }

    QNN_ERR("CreateShareMem::create failed.\n");
    return false;
}

BOOL DeleteShareMem(std::string share_memory_name) {
    ShareMemInfo_t* pShareMemInfo = FindShareMem(share_memory_name);
    if (!pShareMemInfo) {
        QNN_ERR("DeleteShareMem::Cant find this share memory %s.\n", share_memory_name.c_str());
        return false;
    }
    else {
        UnmapViewOfFile(pShareMemInfo->lpBase);
        CloseHandle(pShareMemInfo->hCreateMapFile);
        sg_share_mem_map.erase(share_memory_name);
        free(pShareMemInfo);
        QNN_INF("DeleteShareMem::Count = %d\n", (int)sg_share_mem_map.size());
        return true;
    }

    return false;
}
