//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "LogUtils.hpp"
#include "windows.h"		// zw.


void qnn::log::utils::logCreateLock() {		// zw: We need share the lock between processes.
    sg_logUtilMutex = OpenMutexA(MUTEX_ALL_ACCESS, FALSE, "logStdoutCallbackSharedMutex");
    if (!sg_logUtilMutex) {
        sg_logUtilMutex = CreateMutexA(NULL, FALSE, "logStdoutCallbackSharedMutex");
    }
}

extern std::string g_ProcName;

void qnn::log::utils::logStdoutCallback(const char* fmt,
                                        QnnLog_Level_t level,
                                        uint64_t timestamp,
                                        va_list argp) {
  const char* levelStr = "";
  switch (level) {
    case QNN_LOG_LEVEL_ERROR:
      levelStr = " ERROR ";
      break;
    case QNN_LOG_LEVEL_WARN:
      levelStr = "WARNING";
      break;
    case QNN_LOG_LEVEL_INFO:
      levelStr = "  INFO ";
      break;
    case QNN_LOG_LEVEL_DEBUG:
      levelStr = " DEBUG ";
      break;
    case QNN_LOG_LEVEL_VERBOSE:
      levelStr = "VERBOSE";
      break;
    case QNN_LOG_LEVEL_MAX:
      levelStr = "UNKNOWN";
      break;
  }

  double ms = (double)timestamp / 1000000.0;
  // To avoid interleaved messages
  {		// zw: enhance the log print.
    DWORD dwWaitResult = WaitForSingleObject(sg_logUtilMutex, INFINITE);
    if (WAIT_OBJECT_0 == dwWaitResult) {
        //std::lock_guard<std::mutex> lock(sg_logUtilMutex);
        fprintf(stdout, "%8.1fms [%s][%d][%-7s] ", ms, g_ProcName.c_str(), GetCurrentProcessId(), levelStr);
        vfprintf(stdout, fmt, argp);
        if (fmt[strlen(fmt) - 1] != '\n') {
            fprintf(stdout, "\n");
        }
        fflush(stdout);
    }
    ReleaseMutex(sg_logUtilMutex);
  }
}
