//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#include <cstdarg>
#include <cstdio>
#include <mutex>
#include <string>
#ifdef _WIN32
#include <windows.h>    // zw.
#endif
#include "QnnLog.h"

#ifdef __ANDROID__
#include <fstream>
#include <filesystem>
#endif

namespace qnn {
namespace log {
namespace utils {

void logStdoutCallback(const char* fmt, QnnLog_Level_t level, uint64_t timestamp, va_list argp);
void logCreateLock();

#ifdef __ANDROID__
// Android-specific file logging
void setLogFilePath(const std::string& logPath);
std::string getLogFilePath();
void logFileCallback(const char* fmt, QnnLog_Level_t level, uint64_t timestamp, va_list argp);
#endif

#ifdef _WIN32
static HANDLE sg_logUtilMutex = nullptr;	// zw: We need share the lock between processes.
#else
static std::mutex sg_logUtilMutex;
#endif
}  // namespace utils
}  // namespace log
}  // namespace qnn
