//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
#include <log.h>
#if defined(WIN32)

#include <winsock2.h>

bool isPortAvailable(int port)
{
    WSADATA wsaData;
    SOCKET listenSocket = INVALID_SOCKET;
    sockaddr_in service;

    if (WSAStartup(MAKEWORD(2, 2), &wsaData) != 0)
    {
        My_Log{} << "WSAStartup failed." << std::endl;
        return false;
    }

    listenSocket = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (listenSocket == INVALID_SOCKET)
    {
        My_Log{} << "Error creating socket." << std::endl;
        WSACleanup();
        return false;
    }

    service.sin_family = AF_INET;
    service.sin_addr.s_addr = htonl(INADDR_ANY);
    service.sin_port = htons(port);

    int result = ::bind(listenSocket, (SOCKADDR *) &service, sizeof(service));
    closesocket(listenSocket);
    WSACleanup();

    return result != SOCKET_ERROR;
}

#elif defined(__linux__) && !defined(__ANDROID__)

// Native Linux: implement isPortAvailable via POSIX sockets so the service
// detects port conflicts properly. The Windows branch above and the Android
// stub below both keep their original behaviour.

#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <cerrno>
#include <cstring>

bool isPortAvailable(int port)
{
    int listen_socket = socket(AF_INET, SOCK_STREAM, 0);
    if (listen_socket < 0)
    {
        My_Log{} << "Error creating socket: " << strerror(errno) << std::endl;
        return false;
    }

    int reuse = 1;
    setsockopt(listen_socket, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

    sockaddr_in service{};
    service.sin_family = AF_INET;
    service.sin_addr.s_addr = htonl(INADDR_ANY);
    service.sin_port = htons(port);

    int result = ::bind(listen_socket, reinterpret_cast<sockaddr *>(&service), sizeof(service));
    ::close(listen_socket);

    return result == 0;
}

#else
// Android (and any other POSIX-ish platform that previously took this path):
// keep the original no-op behaviour to avoid changing existing builds.
bool isPortAvailable(int port){return true;}
#endif
