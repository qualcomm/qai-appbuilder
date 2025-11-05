//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include <jni.h>
#include <string>
#include <iostream>
#include <filesystem>
#include <string>
#include <vector>
#include <cstddef>

#include <LibAppBuilder.hpp>
#include <vector>
#include <cstddef>

#ifdef __ANDROID__
#include <android/log.h>
#define LOG_TAG "com.example.genieapiservice"
#define LOGD(...) __android_log_print(ANDROID_LOG_DEBUG, LOG_TAG, __VA_ARGS__)
void My_Log(const std::string& message) {
    LOGD("%s", message.c_str());
}
#else
void My_Log(const std::string& message) {
    std::cout << message << std::endl;
}
#endif

namespace fs = std::filesystem;
const std::string MODEL_NAME = "real_esrgan_x4plus";
const int scale = 4;

extern "C" JNIEXPORT jint JNICALL
Java_com_example_superresolution_MainActivity_SuperResolution(
        JNIEnv* env,
        jobject /* this */,
        jstring j_libsDir,
        jstring j_model_path,
        jobject j_inputBuffer,
        jobject j_outputBuffer) {

    const char* libs_dir_cstr = env->GetStringUTFChars(j_libsDir, nullptr);
    const char* model_path_cstr = env->GetStringUTFChars(j_model_path, nullptr);

    std::string libs_dir(libs_dir_cstr);
    std::string model_path(model_path_cstr);

    float* inputBuffer = (float*)env->GetDirectBufferAddress(j_inputBuffer);
    float* outputBuffer = (float*)env->GetDirectBufferAddress(j_outputBuffer);

    std::string  backend_lib_path = libs_dir + "/libQnnHtp.so";
    std::string  system_lib_path = libs_dir + "/libQnnSystem.so";

    LibAppBuilder libAppBuilder;
    SetLogLevel(3);          //LogLevel::WARN
    SetProfilingLevel(1);    //ProfilingLevel::BASIC

    My_Log("ModelInitialize");
    int ret = libAppBuilder.ModelInitialize(MODEL_NAME, model_path, backend_lib_path, system_lib_path);
    My_Log("ModelInitialize done");
    if(ret == false){
        My_Log("LoadModel failed");
        return -1;
    }
    else {
        My_Log("LoadModel success");
    }

    SetPerfProfileGlobal("burst");

    std::vector<uint8_t*> inputBuffers;
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;

    std::string perfProfile = "burst";//default

    inputBuffers.clear();
    outputBuffers.clear();
    outputSize.clear();

    inputBuffers.push_back(reinterpret_cast<uint8_t*>(inputBuffer));

    My_Log("ModelInference");
    ret = libAppBuilder.ModelInference(MODEL_NAME, inputBuffers, outputBuffers, outputSize, perfProfile);
    My_Log("ModelInference done");
    if(ret == false){
        My_Log("ModelInference failed");
        return -1;
    }
    else {
        My_Log("ModelInference success");
    }

    // Use the data in outputBuffers.
    RelPerfProfileGlobal();

    My_Log("outputBuffers" + std::to_string(outputBuffers.size()));

    memcpy(outputBuffer, outputBuffers.at(0), outputSize[0]);

    // Free the memory in outputBuffers.
    for (int j = 0; j < outputBuffers.size(); j++) {
        free(outputBuffers[j]);
    }
    outputBuffers.clear();
    outputSize.clear();

    libAppBuilder.ModelDestroy(MODEL_NAME);

    env->ReleaseStringUTFChars(j_libsDir, libs_dir_cstr);
    env->ReleaseStringUTFChars(j_model_path, model_path_cstr);

    return 0;
}
