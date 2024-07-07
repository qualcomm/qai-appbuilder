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
#include <windows.h>		// zw.

#include "QnnLog.h"

namespace qnn {
namespace log {
namespace utils {

void logStdoutCallback(const char* fmt, QnnLog_Level_t level, uint64_t timestamp, va_list argp);
void logCreateLock();
// static std::mutex sg_logUtilMutex;		// zw.
static HANDLE sg_logUtilMutex = nullptr;	// zw: We need share the lock between processes.

}  // namespace utils
}  // namespace log
}  // namespace qnn
