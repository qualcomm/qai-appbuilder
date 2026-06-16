//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
//
// IpcChannel - the parent<->child command channel.
//
//   Windows : a pair of anonymous pipes (one per direction), matching the
//             original CreatePipe/WriteFile/ReadFile design.
//   POSIX   : a single AF_UNIX SOCK_SEQPACKET socketpair. SEQPACKET preserves
//             message boundaries (so one Write maps to one Read, the same
//             assumption the original pipe code relied on) and lets us hand a
//             shared-memory fd to the peer via SCM_RIGHTS, delivered atomically
//             with the message it accompanies. This is required on Android,
//             where shared memory cannot be opened by name.
//
//==============================================================================

#pragma once

#ifndef _LIBAPPBUILDER_IPC_CHANNEL_H
#define _LIBAPPBUILDER_IPC_CHANNEL_H

#include "Platform.hpp"

#include <cstring>
#include <deque>
#include <memory>

#ifndef _WIN32
#include <sys/socket.h>
#include <sys/uio.h>
#include <unistd.h>
#include <fcntl.h>
#include <cerrno>
#endif

namespace ipc {

// Endpoint values handed to the child process via its command line, used to
// reconstruct the channel on the child side. On POSIX both fields carry the
// same socket fd (the channel is a single bidirectional socket).
struct ChildEndpoints {
    uint64_t inRead   = 0;   // child reads commands from here
    uint64_t outWrite = 0;   // child writes results here
};

class IpcChannel {
public:
    // Parent side: create the channel and report the endpoint values that must
    // be passed to the child (via SvcProcess::Spawn). The returned channel owns
    // the parent endpoints; call CloseInheritedEnds() after the child is spawned.
    static std::unique_ptr<IpcChannel> CreateParent(ChildEndpoints& outEnds);

    // Child side: reconstruct the channel from the inherited endpoint values.
    static std::unique_ptr<IpcChannel> AttachChild(uint64_t inRead, uint64_t outWrite);

    ~IpcChannel();

    IpcChannel(const IpcChannel&) = delete;
    IpcChannel& operator=(const IpcChannel&) = delete;

    // Send a message. Returns true on success.
    bool Write(const void* data, size_t len);

    // Send a message and, on POSIX, attach a shared-memory fd via SCM_RIGHTS so
    // the peer can map it. On Windows the fd is ignored (shared memory is opened
    // by name) and this behaves exactly like Write().
    bool WriteWithFd(const void* data, size_t len, ShmHandle fd);

    // Receive a message into buf. outRead is set to the number of bytes read.
    // On POSIX, any fd received via SCM_RIGHTS is queued for TakePendingFd().
    bool Read(void* buf, size_t cap, size_t& outRead);

    // POSIX: pop the fd that arrived with the most recently read message(s),
    // FIFO order. Returns kInvalidShm() if none. Windows: always kInvalidShm().
    ShmHandle TakePendingFd();

    // Parent: close the child-side endpoints once the child has been spawned
    // (they remain open across the spawn so the child inherits them).
    void CloseInheritedEnds();

private:
    IpcChannel() = default;

#ifdef _WIN32
    IpcEndpoint m_writeEp = kInvalidEndpoint();   // this side writes here
    IpcEndpoint m_readEp  = kInvalidEndpoint();   // this side reads here
    IpcEndpoint m_childInRead   = kInvalidEndpoint();   // parent only, to close later
    IpcEndpoint m_childOutWrite = kInvalidEndpoint();   // parent only, to close later
#else
    int m_sock      = -1;   // this side's socket
    int m_childSock = -1;   // parent only, child's end to close after spawn
    std::deque<int> m_pendingFds;
#endif
};

//============================================================================
// Windows implementation
//============================================================================
#ifdef _WIN32

inline std::unique_ptr<IpcChannel> IpcChannel::CreateParent(ChildEndpoints& outEnds) {
    SECURITY_ATTRIBUTES saAttr;
    saAttr.nLength = sizeof(SECURITY_ATTRIBUTES);
    saAttr.bInheritHandle = TRUE;
    saAttr.lpSecurityDescriptor = NULL;

    HANDLE hInRead = NULL, hInWrite = NULL;     // parent writes hInWrite, child reads hInRead
    HANDLE hOutRead = NULL, hOutWrite = NULL;   // child writes hOutWrite, parent reads hOutRead

    if (!CreatePipe(&hOutRead, &hOutWrite, &saAttr, 0))
        return nullptr;
    if (!SetHandleInformation(hOutRead, HANDLE_FLAG_INHERIT, 0))   // parent's read end not inherited
        return nullptr;
    if (!CreatePipe(&hInRead, &hInWrite, &saAttr, 0))
        return nullptr;
    if (!SetHandleInformation(hInWrite, HANDLE_FLAG_INHERIT, 0))   // parent's write end not inherited
        return nullptr;

    auto ch = std::unique_ptr<IpcChannel>(new IpcChannel());
    ch->m_writeEp = hInWrite;
    ch->m_readEp  = hOutRead;
    ch->m_childInRead   = hInRead;
    ch->m_childOutWrite = hOutWrite;

    outEnds.inRead   = (uint64_t)hInRead;
    outEnds.outWrite = (uint64_t)hOutWrite;
    return ch;
}

inline std::unique_ptr<IpcChannel> IpcChannel::AttachChild(uint64_t inRead, uint64_t outWrite) {
    auto ch = std::unique_ptr<IpcChannel>(new IpcChannel());
    ch->m_readEp  = (HANDLE)inRead;     // child reads commands
    ch->m_writeEp = (HANDLE)outWrite;   // child writes results
    return ch;
}

inline void IpcChannel::CloseInheritedEnds() {
    if (IsValid(m_childInRead))   { CloseHandle(m_childInRead);   m_childInRead = kInvalidEndpoint(); }
    if (IsValid(m_childOutWrite)) { CloseHandle(m_childOutWrite); m_childOutWrite = kInvalidEndpoint(); }
}

inline IpcChannel::~IpcChannel() {
    CloseInheritedEnds();
    if (IsValid(m_writeEp)) CloseHandle(m_writeEp);
    if (IsValid(m_readEp))  CloseHandle(m_readEp);
}

inline bool IpcChannel::Write(const void* data, size_t len) {
    DWORD dwWrite = 0;
    return WriteFile(m_writeEp, data, (DWORD)len, &dwWrite, NULL) ? true : false;
}

inline bool IpcChannel::WriteWithFd(const void* data, size_t len, ShmHandle /*fd*/) {
    // On Windows the peer opens shared memory by name; no fd to pass.
    return Write(data, len);
}

inline bool IpcChannel::Read(void* buf, size_t cap, size_t& outRead) {
    DWORD dwRead = 0;
    BOOL ok = ReadFile(m_readEp, buf, (DWORD)cap, &dwRead, NULL);
    outRead = (size_t)dwRead;
    return ok ? true : false;
}

inline ShmHandle IpcChannel::TakePendingFd() {
    return kInvalidShm();
}

//============================================================================
// POSIX implementation
//============================================================================
#else

inline std::unique_ptr<IpcChannel> IpcChannel::CreateParent(ChildEndpoints& outEnds) {
    int sv[2] = {-1, -1};
    if (::socketpair(AF_UNIX, SOCK_SEQPACKET, 0, sv) != 0)
        return nullptr;

    // sv[0] = parent end, sv[1] = child end.
    // The child end must survive exec, so do NOT mark it CLOEXEC. The parent end
    // should not leak into the child, so mark it CLOEXEC.
    ::fcntl(sv[0], F_SETFD, FD_CLOEXEC);

    auto ch = std::unique_ptr<IpcChannel>(new IpcChannel());
    ch->m_sock      = sv[0];
    ch->m_childSock = sv[1];

    outEnds.inRead   = (uint64_t)sv[1];
    outEnds.outWrite = (uint64_t)sv[1];   // single bidirectional socket
    return ch;
}

inline std::unique_ptr<IpcChannel> IpcChannel::AttachChild(uint64_t inRead, uint64_t /*outWrite*/) {
    auto ch = std::unique_ptr<IpcChannel>(new IpcChannel());
    ch->m_sock = (int)inRead;   // inRead == outWrite on POSIX
    return ch;
}

inline void IpcChannel::CloseInheritedEnds() {
    if (m_childSock >= 0) { ::close(m_childSock); m_childSock = -1; }
}

inline IpcChannel::~IpcChannel() {
    CloseInheritedEnds();
    if (m_sock >= 0) ::close(m_sock);
    for (int fd : m_pendingFds) ::close(fd);
    m_pendingFds.clear();
}

inline bool IpcChannel::Write(const void* data, size_t len) {
    return WriteWithFd(data, len, kInvalidShm());
}

inline bool IpcChannel::WriteWithFd(const void* data, size_t len, ShmHandle fd) {
    struct msghdr msg;
    std::memset(&msg, 0, sizeof(msg));

    struct iovec iov;
    iov.iov_base = const_cast<void*>(data);
    iov.iov_len  = len;
    msg.msg_iov    = &iov;
    msg.msg_iovlen = 1;

    // Ancillary buffer for one fd.
    char control[CMSG_SPACE(sizeof(int))];
    if (fd >= 0) {
        std::memset(control, 0, sizeof(control));
        msg.msg_control    = control;
        msg.msg_controllen = sizeof(control);

        struct cmsghdr* cmsg = CMSG_FIRSTHDR(&msg);
        cmsg->cmsg_level = SOL_SOCKET;
        cmsg->cmsg_type  = SCM_RIGHTS;
        cmsg->cmsg_len   = CMSG_LEN(sizeof(int));
        std::memcpy(CMSG_DATA(cmsg), &fd, sizeof(int));
    }

    ssize_t n;
    do {
        n = ::sendmsg(m_sock, &msg, 0);
    } while (n < 0 && errno == EINTR);
    return n >= 0;
}

inline bool IpcChannel::Read(void* buf, size_t cap, size_t& outRead) {
    struct msghdr msg;
    std::memset(&msg, 0, sizeof(msg));

    struct iovec iov;
    iov.iov_base = buf;
    iov.iov_len  = cap;
    msg.msg_iov    = &iov;
    msg.msg_iovlen = 1;

    char control[CMSG_SPACE(sizeof(int))];
    std::memset(control, 0, sizeof(control));
    msg.msg_control    = control;
    msg.msg_controllen = sizeof(control);

    ssize_t n;
    do {
        n = ::recvmsg(m_sock, &msg, 0);
    } while (n < 0 && errno == EINTR);

    if (n < 0) {
        outRead = 0;
        return false;
    }
    outRead = (size_t)n;

    // Capture any fd delivered with this message.
    for (struct cmsghdr* cmsg = CMSG_FIRSTHDR(&msg); cmsg != nullptr;
         cmsg = CMSG_NXTHDR(&msg, cmsg)) {
        if (cmsg->cmsg_level == SOL_SOCKET && cmsg->cmsg_type == SCM_RIGHTS) {
            int fd = -1;
            std::memcpy(&fd, CMSG_DATA(cmsg), sizeof(int));
            if (fd >= 0) m_pendingFds.push_back(fd);
        }
    }

    // n == 0 means the peer closed the connection.
    return n > 0;
}

inline ShmHandle IpcChannel::TakePendingFd() {
    if (m_pendingFds.empty()) return kInvalidShm();
    int fd = m_pendingFds.front();
    m_pendingFds.pop_front();
    return fd;
}

#endif  // _WIN32

}  // namespace ipc

#endif  // _LIBAPPBUILDER_IPC_CHANNEL_H
