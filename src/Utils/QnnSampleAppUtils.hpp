//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#include <iostream>
#include <map>
#include <queue>
#include <regex>
#include <string>
#include <tuple>
#include <unordered_map>
#include <vector>
#include "Logger.hpp"

#include "SampleApp.hpp"

namespace qnn {
namespace tools {
namespace sample_app {

enum class ProfilingLevel { OFF, BASIC, DETAILED, INVALID, CLIENT };
enum class ProfilingOption { NONE, OPTRACE, INVALID };

using ReadInputListRetType_t = std::
    tuple<std::vector<std::vector<std::string>>, std::unordered_map<std::string, uint32_t>, bool>;

ReadInputListRetType_t readInputList(std::string inputFileListPath);

using ReadInputListsRetType_t = std::tuple<std::vector<std::vector<std::vector<std::string>>>,
                                           std::vector<std::unordered_map<std::string, uint32_t>>,
                                           bool>;

ReadInputListsRetType_t readInputLists(std::vector<std::string> inputFileListPath);

std::unordered_map<std::string, uint32_t> extractInputNameIndices(const std::string &inputLine,
                                                                  const std::string &separator);

std::string sanitizeTensorName(std::string name);

ProfilingLevel parseProfilingLevel(std::string profilingLevelString);

void parseInputFilePaths(std::vector<std::string> &inputFilePaths,
                         std::vector<std::string> &paths,
                         std::string separator);

void split(std::vector<std::string> &splitString,
           const std::string &tokenizedString,
           const char separator);

bool copyMetadataToGraphsInfo(const QnnSystemContext_BinaryInfo_t *binaryInfo,
                              qnn_wrapper_api::GraphInfo_t **&graphsInfo,
                              uint32_t &graphsCount);

bool copyGraphsInfo(const QnnSystemContext_GraphInfo_t *graphsInput,
                    const uint32_t numGraphs,
                    qnn_wrapper_api::GraphInfo_t **&graphsInfo);

bool copyGraphsInfoV1(const QnnSystemContext_GraphInfoV1_t *graphInfoSrc,
                      qnn_wrapper_api::GraphInfo_t *graphInfoDst);

bool copyGraphsInfoV3(const QnnSystemContext_GraphInfoV3_t *graphInfoSrc,
                      qnn_wrapper_api::GraphInfo_t *graphInfoDst);

bool copyTensorsInfo(const Qnn_Tensor_t *tensorsInfoSrc,
                     Qnn_Tensor_t *&tensorWrappers,
                     uint32_t tensorsCount);

bool deepCopyQnnTensorInfo(Qnn_Tensor_t *dst, const Qnn_Tensor_t *src);

QnnLog_Level_t parseLogLevel(std::string logLevelString);


#ifdef __ANDROID__
#include <android/log.h>
#define LOG_TAG "com.example.genieapiservice"
#define LOGD(...) __android_log_print(ANDROID_LOG_DEBUG, LOG_TAG, __VA_ARGS__)
void inline My_Log(const std::string& message) {
    std::cout << message << std::endl;
    LOGD("%s", message.c_str());
}
#else
void inline My_Log(const std::string& message) {
    std::cout << message << std::endl;
}
#endif

void inline exitWithMessage(std::string &&msg, int code) {
  // std::cerr << msg << std::endl;
  QNN_ERROR(msg.c_str());
  My_Log(msg);
  std::exit(code);
}

}  // namespace sample_app
}  // namespace tools
}  // namespace qnn