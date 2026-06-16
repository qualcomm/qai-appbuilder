//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
//
// Cross-platform primitive type aliases for the SVC IPC abstraction layer.
//
// The remote-inference mechanism (client <-> QAIAppSvc) needs only three OS
// capabilities, each of which has a POSIX counterpart to the Win32 API:
//   - a command channel        (Win: anonymous pipe   / POSIX: socketpair)
//   - a shared memory region    (Win: file mapping     / POSIX: memfd|ashmem + mmap)
//   - a child service process   (Win: CreateProcess    / POSIX: posix_spawn)
//
// This header defines the opaque endpoint/handle types used by IpcChannel,
// SharedRegion and SvcProcess so the rest of the code is platform agnostic.
//
//==============================================================================

#pragma once

#ifndef _LIBAPPBUILDER_IPC_PLATFORM_H
#define _LIBAPPBUILDER_IPC_PLATFORM_H

#include <cstdint>

#ifdef _WIN32

#include <windows.h>

namespace ipc {
// A command-channel endpoint. On Windows this is a pipe HANDLE.
using IpcEndpoint = HANDLE;
// A shared-memory handle. On Windows this is a file-mapping HANDLE.
using ShmHandle   = HANDLE;

inline IpcEndpoint kInvalidEndpoint() { return INVALID_HANDLE_VALUE; }
inline ShmHandle   kInvalidShm()      { return nullptr; }

inline bool IsValid(IpcEndpoint e) { return e != nullptr && e != INVALID_HANDLE_VALUE; }
inline bool IsValidShm(ShmHandle h) { return h != nullptr && h != INVALID_HANDLE_VALUE; }
}  // namespace ipc

#else  // POSIX (Linux / Android)

#include <cstdio>

// Minimal Win32 compatibility shims so the shared SVC code (which uses BOOL /
// TRUE / FALSE / sprintf_s) compiles unchanged on POSIX.
#ifndef BOOL
typedef int BOOL;
#endif
#ifndef TRUE
#define TRUE  1
#endif
#ifndef FALSE
#define FALSE 0
#endif

// sprintf_s(buf, size, fmt, ...) -> snprintf. Variadic macro; bounds-checked.
#ifndef sprintf_s
#define sprintf_s(buf, size, ...) std::snprintf((buf), (size), __VA_ARGS__)
#endif

namespace ipc {
// A command-channel endpoint. On POSIX this is a file descriptor.
using IpcEndpoint = int;
// A shared-memory handle. On POSIX this is a file descriptor.
using ShmHandle   = int;

inline constexpr IpcEndpoint kInvalidEndpoint() { return -1; }
inline constexpr ShmHandle   kInvalidShm()      { return -1; }

inline bool IsValid(IpcEndpoint e)  { return e >= 0; }
inline bool IsValidShm(ShmHandle h) { return h >= 0; }
}  // namespace ipc

#endif  // _WIN32

#endif  // _LIBAPPBUILDER_IPC_PLATFORM_H
