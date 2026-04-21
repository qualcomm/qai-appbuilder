//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "LogUtils.hpp"

#ifdef __ANDROID__
#include <chrono>
#include <iomanip>
#include <sstream>

namespace {
    struct FileLogger {
        std::string log_path_;
        std::ofstream out_;
        std::string tmp_path_;
        const uint32_t kNewOffset_ = 4 * 1024 * 1024;
        const uint32_t kMaxSize_ = 6 * 1024 * 1024;
        std::mutex file_mutex_;
        int write_count_ = 0;
        
        void RotateFile(const std::string& from, const std::string& dst, uint32_t offset) {
            std::ifstream in(from, std::ios::binary);
            std::ofstream out(dst, std::ios::binary | std::ios::trunc);
            in.seekg(offset, std::ios::beg);
            out << in.rdbuf();
            in.close();
            out.close();
        }
    };
    
    FileLogger g_fileLogger;
    bool g_useFileLogging = false;
}

std::string getTimeString() {
    auto now = std::chrono::system_clock::now();
    std::time_t t = std::chrono::system_clock::to_time_t(now);
    auto milliseconds = std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch()) % 1000;
    std::tm tm = *std::localtime(&t);
    std::ostringstream oss;
    oss << std::put_time(&tm, "%Y-%m-%d %H:%M:%S");
    oss << '.' << std::setfill('0') << std::setw(3) << milliseconds.count();
    return oss.str();
}
#endif

void qnn::log::utils::logCreateLock() {		// zw: We need share the lock between processes.
#ifdef _WIN32
    sg_logUtilMutex = OpenMutexA(MUTEX_ALL_ACCESS, FALSE, "logStdoutCallbackSharedMutex");
    if (!sg_logUtilMutex) {
        sg_logUtilMutex = CreateMutexA(NULL, FALSE, "logStdoutCallbackSharedMutex");
    }
#endif
}

#ifdef _WIN32
extern std::string g_ProcName;
#endif

#ifdef __ANDROID__
void qnn::log::utils::setLogFilePath(const std::string& logPath) {
    if (logPath.empty()) {
        g_useFileLogging = false;
        return;
    }
    
    std::lock_guard<std::mutex> lock(g_fileLogger.file_mutex_);
    
    g_fileLogger.log_path_ = logPath;
    g_fileLogger.tmp_path_ = std::filesystem::path{logPath}.parent_path().append("tmp.log").generic_string();
    
    if (g_fileLogger.out_.is_open()) {
        g_fileLogger.out_.close();
    }
    
    g_fileLogger.out_.open(g_fileLogger.log_path_, std::ios::app | std::ios::binary);
    
    if (g_fileLogger.out_.is_open()) {
        g_useFileLogging = true;
        // Write initialization message
        g_fileLogger.out_ << "\n=== Log initialized at " << getTimeString() << " ===\n";
        g_fileLogger.out_.flush();
    } else {
        g_useFileLogging = false;
    }
}

std::string qnn::log::utils::getLogFilePath() {
    std::lock_guard<std::mutex> lock(g_fileLogger.file_mutex_);
    return g_fileLogger.log_path_;
}

void qnn::log::utils::logFileCallback(const char* fmt,
                                       QnnLog_Level_t level,
                                       uint64_t timestamp,
                                       va_list argp) {
    if (!g_useFileLogging || !g_fileLogger.out_.is_open()) {
        return;
    }
    
    const char* levelStr = "";
    switch (level) {
        case QNN_LOG_LEVEL_ERROR:
            levelStr = " [E] ";
            break;
        case QNN_LOG_LEVEL_WARN:
            levelStr = " [W] ";
            break;
        case QNN_LOG_LEVEL_INFO:
            levelStr = " [I] ";
            break;
        case QNN_LOG_LEVEL_DEBUG:
            levelStr = " [D] ";
            break;
        case QNN_LOG_LEVEL_VERBOSE:
            levelStr = " [V] ";
            break;
        default:
            levelStr = " [U] ";
            break;
    }
    
    std::lock_guard<std::mutex> lock(g_fileLogger.file_mutex_);
    
    // Write log entry
    g_fileLogger.out_ << levelStr;
    
    char buffer[2048];
    vsnprintf(buffer, sizeof(buffer), fmt, argp);
    g_fileLogger.out_ << buffer;
    
    if (buffer[strlen(buffer) - 1] != '\n') {
        g_fileLogger.out_ << '\n';
    }
    
    g_fileLogger.out_.flush();
    
    // Check if rotation is needed
    g_fileLogger.write_count_++;
    if (g_fileLogger.write_count_ >= 20 && 
        g_fileLogger.out_.tellp() >= g_fileLogger.kMaxSize_) {
        g_fileLogger.out_.close();
        
        g_fileLogger.RotateFile(g_fileLogger.log_path_,
                               g_fileLogger.tmp_path_,
                               g_fileLogger.kNewOffset_);
        
        g_fileLogger.RotateFile(g_fileLogger.tmp_path_,
                               g_fileLogger.log_path_,
                               0);
        
        g_fileLogger.out_.open(g_fileLogger.log_path_, std::ios::app | std::ios::binary);
        g_fileLogger.write_count_ = 0;
    }
}
#endif

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
#ifdef _WIN32
  {    // zw: enhance the log print.
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
#else
  {
    std::lock_guard<std::mutex> lock(sg_logUtilMutex);
    fprintf(stdout, "%8.1fms [%-7s] ", ms, levelStr);
    vfprintf(stdout, fmt, argp);
    fprintf(stdout, "\n");
  }
#endif
}
