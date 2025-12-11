//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "core/utils.h"
#include <chrono>

#include "core/log.h"

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

#else
bool isPortAvailable(int port){return true;}
#endif

std::atomic<bool> http_busy_{false};

struct TimerHelper::Impl
{
    std::chrono::steady_clock::time_point time_start;
    std::chrono::steady_clock::time_point time_now;
};

TimerHelper::TimerHelper() : impl_{new Impl{}}
{
    Reset();
}

void TimerHelper::Print(const std::string &message)
{
    impl_->time_now = std::chrono::steady_clock::now();
    double dr_ms = std::chrono::duration<double, std::milli>(impl_->time_now - impl_->time_start).count();
    // printf("Time: %s %.2f ms\n", message.c_str(), dr_ms);
    My_Log{} << YELLOW << std::fixed << std::setprecision(2) << dr_ms
             << " ms" << RESET << std::endl;
}

void TimerHelper::Print(std::string message, bool reset)
{
    Print(message);
    if (reset)
    {
        Reset();
    }
}

long long TimerHelper::GetSystemTime()
{
    auto now = std::chrono::system_clock::now();
    auto duration = now.time_since_epoch();
    auto milliseconds = std::chrono::duration_cast<std::chrono::milliseconds>(duration).count();

    return milliseconds;
}

void TimerHelper::Reset()
{
    impl_->time_start = std::chrono::steady_clock::now();
}

TimerHelper timer;

std::unique_ptr<int8_t[]> File::get_file_as_buffer(const std::string &file_path, uint32_t &size)
{
    std::ifstream in(file_path, std::ifstream::binary | std::ios::ate);
    size = in.tellg();
    if (size == 0)
        return nullptr;
    auto buf = std::make_unique<int8_t[]>(size);
    in.read(reinterpret_cast<char *>(buf.get()), size);
    return buf;
}

size_t File::get_file_size(const std::string &file_path)
{
    std::ifstream file(file_path, std::ios::binary | std::ios::ate);
    return file.tellg();
}

bool File::IsFileEmpty(const std::string &file_path)
{
    std::ifstream f;
//    f.exceptions(std::ifstream::failbit | std::ifstream::badbit);
    f.open(file_path.c_str(), std::ios::in | std::ios::binary | std::ios::ate);
    return f.tellg() == 0;
}

bool File::IsFileExist(const std::string &file_path)
{
    return std::ifstream(file_path.c_str()).good();
}
