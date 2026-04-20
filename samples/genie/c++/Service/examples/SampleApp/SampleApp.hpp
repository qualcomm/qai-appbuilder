//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
#ifndef SAMPLEAPP_HPP
#define SAMPLEAPP_HPP

#pragma once

// #include "platform_fix.h"
#include <fstream>
#include <iostream>
#include <string>
#include <random>
#include <windows.h>
#include <chrono>
#include <filesystem>
#include <iomanip>
#include <thread>
#include "GenieAPILibrary.h"

#define MODEL_TYPE_QWEN        0
#define MODEL_TYPE_LLAMA3      1
#define MODEL_TYPE_QWEN2       2
#define MODEL_TYPE_QWEN2_5     3
#define MODEL_TYPE_LLAMA3_1    4
#define MODEL_TYPE_PHI4MM      5
#define MODEL_TYPE           MODEL_TYPE_PHI4MM
#define PUBLIC_MODEL         1

#if MODEL_TYPE == MODEL_TYPE_LLAMA3
#define FEATURE_LLAMA_PROMPT    1
#endif

namespace fs = std::filesystem;

extern double sg_prompt_evaluation_speed;
extern double sg_average_token_per_second;

size_t get_file_size(std::string filePath)
{
    std::ifstream in(filePath, std::ifstream::binary);
    if (!in)
    {
        printf("Failed to open file: %s\n", filePath.c_str());
        return 0;
    }
    in.seekg(0, in.end);
    const size_t length = in.tellg();
    in.seekg(0, in.beg);
    return length;
}


/////////////////////////////////////////////////////////////////////////////
/// Class TimerHelper declaration.
/////////////////////////////////////////////////////////////////////////////
#pragma warning(disable:4251)

class TimerHelper
{
public:
    TimerHelper()
    {
        Reset();
    }

    void Reset()
    {
        time_start = std::chrono::system_clock::now();
        time_diff_start = std::chrono::steady_clock::now();
    }

    void PrintDiff(std::string message, bool reset = false)
    {
        time_diff_now = std::chrono::steady_clock::now();
        double dr_ms = std::chrono::duration<double, std::milli>(time_diff_now - time_diff_start).count();
        dr_ms_total += dr_ms;
        count++;
        printf("Time: %s cur %.2f ms | avg %.2f ms\n", message.c_str(), dr_ms, dr_ms_total / count);

        if (reset)
        {
            Reset();
        }
    }

    void Print(std::string message)
    {
        time_now = std::chrono::system_clock::now();

        std::time_t start = std::chrono::system_clock::to_time_t(time_start);
        std::time_t now = std::chrono::system_clock::to_time_t(time_now);

        std::tm start_tm = *std::localtime(&start);
        std::tm now_tm = *std::localtime(&now);

        std::cout << "============= [" << std::setfill('0') << std::setw(2) << start_tm.tm_hour << ":"
                  << std::setfill('0') << std::setw(2) << start_tm.tm_min << ":"
                  << std::setfill('0') << std::setw(2) << start_tm.tm_sec << " - ";

        std::cout << std::setfill('0') << std::setw(2) << now_tm.tm_hour << ":"
                  << std::setfill('0') << std::setw(2) << now_tm.tm_min << ":"
                  << std::setfill('0') << std::setw(2) << now_tm.tm_sec << "] =============" << std::endl;
    }

    void Print(std::string message, bool reset)
    {
        Print(message);
        if (reset)
        {
            Reset();
        }
    }

private:
    std::chrono::system_clock::time_point time_start;
    std::chrono::system_clock::time_point time_now;

    std::chrono::steady_clock::time_point time_diff_start;
    std::chrono::steady_clock::time_point time_diff_now;

    double dr_ms_total = 0;
    int count = 0;
};

#endif
