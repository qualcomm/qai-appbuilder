//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
//
// SharedRegion - a shared-memory region used to ferry tensor data between the
// client (libappbuilder) and the QAIAppSvc service process with zero copies.
//
//   Windows : CreateFileMappingA / OpenFileMappingA + MapViewOfFile. The region
//             is shared by NAME: the client creates it, the server opens the
//             same name.
//   POSIX   : the region is backed by an fd (Android: ASharedMemory_create;
//             Linux: memfd_create) and mmap'd. fds cannot be looked up by name,
//             so the client passes the fd to the server per inference via the
//             IpcChannel (SCM_RIGHTS) and the server maps it with OpenFromHandle.
//
// Both platforms expose the same Base()/Size() accessors, so the protocol
// (name + size + offset/size encoding carried in the command string) is
// unchanged across platforms.
//
//==============================================================================

#pragma once

#ifndef _LIBAPPBUILDER_IPC_SHARED_REGION_H
#define _LIBAPPBUILDER_IPC_SHARED_REGION_H

#include "Platform.hpp"

#include <cstdint>
#include <memory>
#include <string>

#ifndef _WIN32
#include <sys/mman.h>
#include <unistd.h>
#include <fcntl.h>
#include <cstring>
#if defined(__ANDROID__)
#include <android/sharedmem.h>
#else
#include <sys/syscall.h>
#endif
#endif

namespace ipc {

class SharedRegion {
public:
    // Client side: create a new named region of the given size.
    static std::unique_ptr<SharedRegion> Create(const std::string& name, size_t size);

    // Server side (Windows): open an existing region by name.
    // On POSIX this is unsupported (returns nullptr) - use OpenFromHandle.
    static std::unique_ptr<SharedRegion> OpenByName(const std::string& name, size_t size);

    // Server side (POSIX): map a region from an fd received over the channel.
    // On Windows this is unsupported (returns nullptr) - use OpenByName.
    static std::unique_ptr<SharedRegion> OpenFromHandle(ShmHandle handle, size_t size);

    ~SharedRegion();

    SharedRegion(const SharedRegion&) = delete;
    SharedRegion& operator=(const SharedRegion&) = delete;

    uint8_t*  Base() const   { return reinterpret_cast<uint8_t*>(m_base); }
    size_t    Size() const   { return m_size; }

    // The underlying OS handle. On POSIX this fd is what the client attaches to
    // the inference command (channel->WriteWithFd). On Windows it is the mapping
    // handle (not transmitted; the peer opens by name).
    ShmHandle Handle() const { return m_handle; }

private:
    SharedRegion() = default;

    void*     m_base   = nullptr;
    size_t    m_size   = 0;
    ShmHandle m_handle = kInvalidShm();
#ifdef _WIN32
    bool      m_ownsHandle = true;
#endif
};

//============================================================================
// Windows implementation
//============================================================================
#ifdef _WIN32

inline std::unique_ptr<SharedRegion> SharedRegion::Create(const std::string& name, size_t size) {
    HANDLE h = CreateFileMappingA(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE, 0,
                                  (DWORD)size, name.c_str());
    if (!h) return nullptr;

    LPVOID base = MapViewOfFile(h, FILE_MAP_ALL_ACCESS, 0, 0, (SIZE_T)size);
    if (!base) {
        CloseHandle(h);
        return nullptr;
    }

    auto r = std::unique_ptr<SharedRegion>(new SharedRegion());
    r->m_handle = h;
    r->m_base   = base;
    r->m_size   = size;
    return r;
}

inline std::unique_ptr<SharedRegion> SharedRegion::OpenByName(const std::string& name, size_t size) {
    HANDLE h = OpenFileMappingA(FILE_MAP_ALL_ACCESS, FALSE, name.c_str());
    if (!h) return nullptr;

    LPVOID base = MapViewOfFile(h, FILE_MAP_ALL_ACCESS, 0, 0, 0);
    if (!base) {
        CloseHandle(h);
        return nullptr;
    }

    auto r = std::unique_ptr<SharedRegion>(new SharedRegion());
    r->m_handle = h;
    r->m_base   = base;
    r->m_size   = size;
    return r;
}

inline std::unique_ptr<SharedRegion> SharedRegion::OpenFromHandle(ShmHandle, size_t) {
    return nullptr;  // not used on Windows
}

inline SharedRegion::~SharedRegion() {
    if (m_base) UnmapViewOfFile(m_base);
    if (IsValidShm(m_handle)) CloseHandle(m_handle);
}

//============================================================================
// POSIX implementation
//============================================================================
#else

inline std::unique_ptr<SharedRegion> SharedRegion::Create(const std::string& name, size_t size) {
    int fd = -1;
#if defined(__ANDROID__)
    // ASharedMemory_create is available from API level 26.
    fd = ASharedMemory_create(name.c_str(), size);
#else
    // memfd_create gives an anonymous, sealed-capable, mmap-able fd on Linux.
    fd = (int)::syscall(SYS_memfd_create, name.c_str(), 0u);
    if (fd >= 0) {
        if (::ftruncate(fd, (off_t)size) != 0) {
            ::close(fd);
            fd = -1;
        }
    }
#endif
    if (fd < 0) return nullptr;

    void* base = ::mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (base == MAP_FAILED) {
        ::close(fd);
        return nullptr;
    }

    auto r = std::unique_ptr<SharedRegion>(new SharedRegion());
    r->m_handle = fd;
    r->m_base   = base;
    r->m_size   = size;
    return r;
}

inline std::unique_ptr<SharedRegion> SharedRegion::OpenByName(const std::string&, size_t) {
    return nullptr;  // POSIX shares by fd, not by name
}

inline std::unique_ptr<SharedRegion> SharedRegion::OpenFromHandle(ShmHandle handle, size_t size) {
    if (handle < 0) return nullptr;

    void* base = ::mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED, handle, 0);
    if (base == MAP_FAILED) {
        ::close(handle);
        return nullptr;
    }

    auto r = std::unique_ptr<SharedRegion>(new SharedRegion());
    r->m_handle = handle;   // takes ownership of the received fd
    r->m_base   = base;
    r->m_size   = size;
    return r;
}

inline SharedRegion::~SharedRegion() {
    if (m_base && m_base != MAP_FAILED) ::munmap(m_base, m_size);
    if (IsValidShm(m_handle)) ::close(m_handle);
}

#endif  // _WIN32

}  // namespace ipc

#endif  // _LIBAPPBUILDER_IPC_SHARED_REGION_H
