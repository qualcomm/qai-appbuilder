//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include <chrono>
#include <filesystem>

#include "log.h"
#include "utils.h"

namespace fs = std::filesystem;

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

struct Timer::Impl
{
    std::chrono::steady_clock::time_point time_start;
    std::chrono::steady_clock::time_point time_now;
};

Timer::Timer() : impl_{new Impl{}}
{
    Reset();
}

void Timer::Print(const std::string &message)
{
    impl_->time_now = std::chrono::steady_clock::now();
    double dr_ms = std::chrono::duration<double, std::milli>(impl_->time_now - impl_->time_start).count();
    // printf("Time: %s %.2f ms\n", message.c_str(), dr_ms);
    My_Log{} << YELLOW << std::fixed << std::setprecision(2) << dr_ms
             << " ms" << RESET << std::endl;
}

void Timer::Print(std::string message, bool reset)
{
    Print(message);
    if (reset)
    {
        Reset();
    }
}

long long Timer::GetSystemTime()
{
    auto now = std::chrono::system_clock::now();
    auto duration = now.time_since_epoch();
    auto milliseconds = std::chrono::duration_cast<std::chrono::milliseconds>(duration).count();

    return milliseconds;
}

void Timer::Reset()
{
    impl_->time_start = std::chrono::steady_clock::now();
}

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

size_t File::get_file_size(const std::string &file_path, std::ios::openmode mode)
{
    std::ifstream file(file_path, mode | std::ios::ate);
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

bool File::MatchFileInDir(const std::string &dir_path, const std::string &file, bool is_ext)
{
    /* @formatter:off */
    auto checker = is_ext ?
                   std::function<bool(const fs::directory_entry&)>{[&file](const fs::directory_entry &entry)
                   {
                       return entry.path().extension() == file;
                   }} :
                   [&file](const fs::directory_entry &entry)
                   {
                       return entry.path() == file;
                   };
    /* @formatter:on */

    for (const auto &entry: fs::directory_iterator(dir_path))
    {
        if (entry.is_regular_file() && checker(entry))
        {
            return true;
        }
    }
    return false;
}

std::vector<std::string> File::SearchExtInDir(const std::string &dir_path, const std::string &ext)
{
    std::vector<std::string> files;
    for (const auto &entry: fs::directory_iterator(dir_path))
    {
        if (entry.is_regular_file() && entry.path().extension() == ext)
        {
            files.push_back(entry.path().generic_string());
        }
    }
    return files;
}

std::vector<uint8_t> File::ReadFile(const std::string &file_name, bool binary)
{
    auto mode = binary ? std::ios::binary : std::ios::in;
    std::ifstream in(file_name, mode);
    auto file_size = File::get_file_size(file_name, mode);
    std::vector<uint8_t> buffer(file_size);
    in.read(reinterpret_cast<char *>(buffer.data()), file_size);
    return buffer;
}