//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
//
// SvcProcess - launches the QAIAppSvc service process and hands it the command
// channel endpoints plus logging parameters via its command line.
//
//   Windows : CreateProcess. The command line is
//               QAIAppSvc.exe svc <inRead> <outWrite> <epoch> <lvl> <prof> "name"
//             and the pipe handles are inherited (bInheritHandle == TRUE).
//   POSIX   : posix_spawn of ./QAIAppSvc with argv:
//               QAIAppSvc svc <sockFd> <sockFd> <epoch> <lvl> <prof> name
//             The child socket fd is inherited because it is NOT marked
//             CLOEXEC (see IpcChannel::CreateParent).
//
// The argv layout is identical across platforms so main.cpp parses it the same
// way; only the meaning of the endpoint integers differs (HANDLE vs fd).
//
//==============================================================================

#pragma once

#ifndef _LIBAPPBUILDER_IPC_SVC_PROCESS_H
#define _LIBAPPBUILDER_IPC_SVC_PROCESS_H

#include "Platform.hpp"
#include "IpcChannel.hpp"

#include <cstdio>
#include <memory>
#include <string>

#ifndef _WIN32
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>
#include <cerrno>
#include <vector>
extern char** environ;
#endif

namespace ipc {

class SvcProcess {
public:
    // Launch the service. exePath is the executable name/path (e.g.
    // "QAIAppSvc.exe" on Windows, "./QAIAppSvc" on POSIX). ends carries the
    // channel endpoints the child must inherit.
    static std::unique_ptr<SvcProcess> Spawn(const std::string& exePath,
                                             const std::string& proc_name,
                                             const ChildEndpoints& ends,
                                             uint64_t logEpoch, int logLevel, int profLevel);

    ~SvcProcess();

    SvcProcess(const SvcProcess&) = delete;
    SvcProcess& operator=(const SvcProcess&) = delete;

    // Wait for / reap the child. The actual "tell the child to exit" signal is
    // closing the command channel (done by destroying the IpcChannel), which
    // makes the child's Read() return EOF and its loop break.
    void Wait();

private:
    SvcProcess() = default;

#ifdef _WIN32
    PROCESS_INFORMATION m_pi{};
#else
    pid_t m_pid = -1;
#endif
};

//============================================================================
// Windows implementation
//============================================================================
#ifdef _WIN32

inline std::unique_ptr<SvcProcess> SvcProcess::Spawn(const std::string& exePath,
                                                     const std::string& proc_name,
                                                     const ChildEndpoints& ends,
                                                     uint64_t logEpoch, int logLevel, int profLevel) {
    char cmdline[4096];
    _snprintf_s(cmdline, sizeof(cmdline), _TRUNCATE,
                "%s svc %llu %llu %llu %d %d \"%s\"",
                exePath.c_str(),
                (unsigned long long)ends.inRead, (unsigned long long)ends.outWrite,
                (unsigned long long)logEpoch, logLevel, profLevel, proc_name.c_str());

    STARTUPINFOA si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    ZeroMemory(&pi, sizeof(pi));
    si.cb = sizeof(si);

    BOOL ok = CreateProcessA(NULL, cmdline, NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi);
    if (!ok) return nullptr;

    auto p = std::unique_ptr<SvcProcess>(new SvcProcess());
    p->m_pi = pi;
    return p;
}

inline void SvcProcess::Wait() {
    if (m_pi.hProcess) {
        WaitForSingleObject(m_pi.hProcess, INFINITE);
    }
}

inline SvcProcess::~SvcProcess() {
    if (m_pi.hProcess) CloseHandle(m_pi.hProcess);
    if (m_pi.hThread)  CloseHandle(m_pi.hThread);
}

//============================================================================
// POSIX implementation
//============================================================================
#else

inline std::unique_ptr<SvcProcess> SvcProcess::Spawn(const std::string& exePath,
                                                     const std::string& proc_name,
                                                     const ChildEndpoints& ends,
                                                     uint64_t logEpoch, int logLevel, int profLevel) {
    char inRead[32], outWrite[32], epoch[32], lvl[16], prof[16];
    std::snprintf(inRead,   sizeof(inRead),   "%llu", (unsigned long long)ends.inRead);
    std::snprintf(outWrite, sizeof(outWrite), "%llu", (unsigned long long)ends.outWrite);
    std::snprintf(epoch,    sizeof(epoch),    "%llu", (unsigned long long)logEpoch);
    std::snprintf(lvl,      sizeof(lvl),      "%d",   logLevel);
    std::snprintf(prof,     sizeof(prof),     "%d",   profLevel);

    std::string exe = exePath;
    std::string name = proc_name;
    char* argv[] = {
        const_cast<char*>(exe.c_str()),
        const_cast<char*>("svc"),
        inRead, outWrite, epoch, lvl, prof,
        const_cast<char*>(name.c_str()),
        nullptr
    };

    pid_t pid = -1;
    // posix_spawnp searches PATH for exe (like CreateProcess on Windows), so a
    // bare "QAIAppSvc" resolves to the binary shipped on PATH by the wrapper.
    int rc = ::posix_spawnp(&pid, exe.c_str(), nullptr, nullptr, argv, environ);
    if (rc != 0) return nullptr;

    auto p = std::unique_ptr<SvcProcess>(new SvcProcess());
    p->m_pid = pid;
    return p;
}

inline void SvcProcess::Wait() {
    if (m_pid > 0) {
        int status = 0;
        pid_t r;
        do {
            r = ::waitpid(m_pid, &status, 0);
        } while (r < 0 && errno == EINTR);
        m_pid = -1;
    }
}

inline SvcProcess::~SvcProcess() {
    // The command channel is closed before this runs (see ProcInfo_t member
    // order), so the child has received EOF and is exiting; reap it. waitpid
    // blocks only briefly because the child's read loop breaks immediately.
    Wait();
}

#endif  // _WIN32

}  // namespace ipc

#endif  // _LIBAPPBUILDER_IPC_SVC_PROCESS_H
