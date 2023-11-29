//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

// #define _CRTDBG_MAP_ALLOC       // TODO: Debug memory leak.
// #include <stdlib.h>
// #include <crtdbg.h>

#include "Utils/Utils.hpp"


// ============================== Service / SvcQNNHelper ============================== //

#define ACTION_OK        "OK"
#define ACTION_FAILED    "Failed"

LibQNNHelper g_LibQNNHelper;

LPVOID OpenShareMem(std::string share_memory_name, size_t share_memory_size) {
    HANDLE hOpenMapFile = nullptr;
    LPVOID lpBase = nullptr;
    ShareMemInfo_t* pShareMemInfo = nullptr;

    hOpenMapFile = OpenFileMappingA(FILE_MAP_ALL_ACCESS, NULL, share_memory_name.c_str());

    if (hOpenMapFile) {
        lpBase = MapViewOfFile(hOpenMapFile, FILE_MAP_ALL_ACCESS, 0, 0, 0);
    }
    if (lpBase) {
        pShareMemInfo = (ShareMemInfo_t*)malloc(sizeof(ShareMemInfo_t));

        if (pShareMemInfo) {
            pShareMemInfo->hCreateMapFile = hOpenMapFile;
            pShareMemInfo->lpBase = lpBase;
            pShareMemInfo->size = share_memory_size;
            // QNN_INF("OpenShareMem::pShareMemInfo %p\n", pShareMemInfo);
            sg_share_mem_map.insert(std::make_pair(share_memory_name, pShareMemInfo));
        }
    }

    return lpBase;
}

void CloseShareMem(std::string share_memory_name) {
    ShareMemInfo_t* pShareMemInfo = sg_share_mem_map[share_memory_name];

    if (pShareMemInfo) {
        // QNN_INF("CloseShareMem::pShareMemInfo %p\n", pShareMemInfo);
        UnmapViewOfFile(pShareMemInfo->lpBase);
        CloseHandle(pShareMemInfo->hCreateMapFile);
        sg_share_mem_map.erase(share_memory_name);
        free(pShareMemInfo);
    }
    else {
        QNN_ERR("CloseShareMem::Can't find share memory%s.\n", share_memory_name.c_str());
    }
}

void ModelLoad(std::string cmdBuf, HANDLE hSvcPipeOutWrite) {
    BOOL bSuccess;
    Print_MemInfo("ModelLoad Start.");

    std::vector<std::string> commands;
    split_string(commands, cmdBuf, ';');

    std::string model_name                  = commands[0];
    std::string model_path                  = commands[1];
    std::string backend_lib_path            = commands[2];
    std::string system_lib_path             = commands[3];
    std::string backend_ext_lib_path        = commands[4];
    std::string backend_ext_config_path     = commands[5];

    Print_MemInfo("ModelLoad::ModelInitialize Start.");
    QNN_INF("ModelLoad::ModelInitialize::Model name %s\n", model_name.c_str());
    bSuccess = g_LibQNNHelper.ModelInitialize(model_name.c_str(), model_path, backend_lib_path, system_lib_path, backend_ext_lib_path, backend_ext_config_path);
    QNN_INF("ModelLoad::ModelInitialize End ret = %d\n", bSuccess);
    Print_MemInfo("ModelLoad::ModelInitialize End.");

    if(bSuccess) {
        bSuccess = WriteFile(hSvcPipeOutWrite, ACTION_OK, (DWORD)strlen(ACTION_OK) + 1, NULL, NULL);
    }
    else {
        bSuccess = WriteFile(hSvcPipeOutWrite, ACTION_FAILED, (DWORD)strlen(ACTION_FAILED) + 1, NULL, NULL);
    }
}

void ModelRun(std::string cmdBuf, HANDLE hSvcPipeOutWrite) {
    BOOL bSuccess;
    Print_MemInfo("ModelRun Start.");
    // TimerHelper timerHelper;

    std::vector<std::string> commands;
    split_string(commands, cmdBuf, ';');

    std::string model_name        = commands[0];
    std::string share_memory_name = commands[1];
    size_t share_memory_size      = std::stoull(commands[2]);
    std::string strBufferArray      = commands[3];

    // Open share memory and read the inference data from share memory.
    LPVOID lpBase = OpenShareMem(share_memory_name, share_memory_size);

    std::vector<uint8_t*> inputBuffers;
    std::vector<size_t> inputSize;
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;
    outputSize.push_back(12345);

    // Fill data from 'pShareMemInfo->lpBase' to 'inputBuffers' vector before inference the model.
    // TODO: check if can optimize it
    ShareMemToVector(strBufferArray, (uint8_t*)lpBase, inputBuffers, inputSize);

    Print_MemInfo("ModelRun::ModelInference Start.");
    //QNN_INF("ModelRun::ModelInference %s\n", model_name.c_str());
    bSuccess = g_LibQNNHelper.ModelInference(model_name.c_str(), inputBuffers, outputBuffers, outputSize);
    //QNN_INF("ModelRun::ModelInference End ret = %d\n", bSuccess);
    Print_MemInfo("ModelRun::ModelInference End.");

    // TODO: fill data from outputBuffers to 'pShareMemInfo->lpBase' and send back to client.
    std::pair<std::string, std::string> strResultArray = VectorToShareMem(share_memory_size, (uint8_t*)lpBase, outputBuffers, outputSize);

    outputBuffers.clear();
    outputSize.clear();

    Print_MemInfo("ModelRun::CloseShareMem Start.");
    CloseShareMem(share_memory_name);
    Print_MemInfo("ModelRun::CloseShareMem End.");

    // timerHelper.Print("ModelRun");

    DWORD dwRead = 0, dwWrite = 0;
    std::string command = strResultArray.first + "=" + strResultArray.second;
    dwRead = (DWORD)command.length() + 1;
    if (bSuccess) {
        bSuccess = WriteFile(hSvcPipeOutWrite, command.c_str(), dwRead, &dwWrite, NULL);
    }
    else {
        bSuccess = WriteFile(hSvcPipeOutWrite, ACTION_FAILED, (DWORD)strlen(ACTION_FAILED) + 1, NULL, NULL);
    }
}

void ModelRelease(std::string cmdBuf, HANDLE hSvcPipeOutWrite) {
    BOOL bSuccess;
    Print_MemInfo("ModelRelease Start.");

    std::vector<std::string> commands;
    split_string(commands, cmdBuf, ';');

    std::string model_name = commands[0];

    Print_MemInfo("ModelRelease::ModelDestroy Start.");
    QNN_INF("ModelRelease::ModelDestroy %s\n", model_name.c_str());
    bSuccess = g_LibQNNHelper.ModelDestroy(model_name.c_str());
    QNN_INF("ModelRelease::ModelDestroy End ret = %d\n", bSuccess);
    Print_MemInfo("ModelRelease::ModelDestroy End.");

    if (bSuccess) {
        bSuccess = WriteFile(hSvcPipeOutWrite, ACTION_OK, (DWORD)strlen(ACTION_OK) + 1, NULL, NULL);
    }
    else {
        bSuccess = WriteFile(hSvcPipeOutWrite, ACTION_FAILED, (DWORD)strlen(ACTION_FAILED) + 1, NULL, NULL);
    }
}

int svcprocess_run(HANDLE hSvcPipeInRead, HANDLE hSvcPipeOutWrite) {
    DWORD dwRead = 0, dwWrite = 0;
    BOOL bSuccess = false;

    if ((hSvcPipeOutWrite == INVALID_HANDLE_VALUE) || (hSvcPipeInRead == INVALID_HANDLE_VALUE)) {
        ErrorExit("Svc::Failed to get write or read handle.");
    }

    for (;;) {
        bSuccess = ReadFile(hSvcPipeInRead, g_buffer, GLOBAL_BUFSIZE, &dwRead, NULL);

        if (!bSuccess || dwRead == 0) {
            QNN_WAR("Svc::Failed to read from hSvcPipeInRead, perhaps parent process closed pipe or died.");
            break;
        }

        char* cmdBuf = g_buffer + 1;
        switch (g_buffer[0]) {
            case 'l':   // load model.
                ModelLoad(cmdBuf, hSvcPipeOutWrite);
                break;

            case 'g':   // run Graphs.
                ModelRun(cmdBuf, hSvcPipeOutWrite);
                break;

            case 'r':   // release model.
                ModelRelease(cmdBuf, hSvcPipeOutWrite);
                break;
        }
    }

    return 0;
}


// ============================== Client / libQNNHelper ============================== //
#define MODEL_SHAREMEM_SIZE      (1024 * 1024 * 50)  // 50M

#define MODEL_DATA_PATH_IN       "C:\\Source\\Projects\\QNNHelper\\build\\Release\\temp\\input_%d.raw"
#define MODEL_DATA_PATH_OUT      "C:\\Source\\Projects\\QNNHelper\\build\\Release\\temp\\output_%d.raw"
#define MODEL_DATA_INPUT_CNT     1
#define BUFSIZE                 256

int hostprocess_run() {
    BOOL result = false;

    std::string MODEL_NAME = "unet";
    std::string PROC_NAME = "~unet";
    std::string WORK_PATH = "C:\\Source\\SD_QC\\ControlNet\\controlnet_workspace_V2\\qnn_assets\\";
    std::string qnn_binary_path = WORK_PATH + "QNN_binaries\\";

    std::string model_memory_name = MODEL_NAME;
    std::string model_name = MODEL_NAME;
    std::string proc_name = PROC_NAME;
    std::string model_path = WORK_PATH + "models\\realesrgan\\realesrgan_x4_512_quantized.serialized.v68.bin";
    std::string backend_lib_path = qnn_binary_path + "QnnHtp.dll";
    std::string system_lib_path = qnn_binary_path + "QnnSystem.dll";
    std::string backend_ext_lib_path = qnn_binary_path + "QnnHtpNetRunExtensions.dll";
    std::string backend_ext_config_path = qnn_binary_path + "htp_backend_ext_config_V68.json";


    QNN_INF("Load data from raw data file to vector Start.\n");
    std::vector<uint8_t*> inputBuffers;
    std::vector<size_t> inputSize;
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;
    char dataPath[BUFSIZE];

    for (int i = 0; i < MODEL_DATA_INPUT_CNT; i++) {
        sprintf_s(dataPath, BUFSIZE, MODEL_DATA_PATH_IN, i);
        std::ifstream in(dataPath, std::ifstream::binary);
        if (!in) {
            QNN_ERR("Failed to open input file: %s", dataPath);
        }
        else {
            uint8_t* buffer = nullptr;
            in.seekg(0, in.end);
            const size_t length = in.tellg();
            in.seekg(0, in.beg);
            buffer = (uint8_t*)malloc(length);
            if (!in.read(reinterpret_cast<char*>(buffer), length)) {
                QNN_ERR("Failed to read the of: %s", dataPath);
            }

            inputBuffers.push_back(reinterpret_cast<uint8_t*>(buffer));
            inputSize.push_back(length);
        }
        in.close();
    }
    QNN_INF("Load data from raw data file to vector End.\n");

    Print_MemInfo("Load data from raw data file End.");
    LibQNNHelper libQNNHelper;

    // TODO: test code, load and run model locally.
//#define TEST_MODEL_LOCAL        1
#ifdef TEST_MODEL_LOCAL
#define INFERENCE_TIMES     (1)
    {
        QNN_INF("Load and run model locally Start.\n");
        result = libQNNHelper.ModelInitialize(model_name, model_path, backend_lib_path, system_lib_path, backend_ext_lib_path, backend_ext_config_path);
        Print_MemInfo("ModelInitialize End.");
        
        for(int i = 0; i < INFERENCE_TIMES; i++) {
            result = libQNNHelper.ModelInference(model_name, inputBuffers, outputBuffers, outputSize);
            for (int j = 0; j < outputBuffers.size(); j++) {
                free(outputBuffers[j]);
            }
            outputBuffers.clear();
            outputSize.clear();
            Print_MemInfo("ModelInference End." + std::to_string(i));
        }

        result = libQNNHelper.ModelDestroy(model_name);
        QNN_INF("Load and run model locally End.\n");
        Print_MemInfo("Load and run model locally End.");
    }
#endif

    libQNNHelper.CreateShareMemory(model_memory_name, MODEL_SHAREMEM_SIZE);
    // memset(pShareMemInfo->lpBase, 0, MODEL_SHAREMEM_SIZE);

    // Add 'TalkToSvc_*' function to 'libQNNHelper'.
    QNN_INF("TalkToSvc_Initialize Start.\n");
    result = libQNNHelper.ModelInitialize(model_name, proc_name, model_path, backend_lib_path, system_lib_path, backend_ext_lib_path, backend_ext_config_path);
    QNN_INF("TalkToSvc_Initialize End %d.\n", result);

    QNN_INF("TalkToSvc_Inference Start.\n");
    result = libQNNHelper.ModelInference(model_name, proc_name, model_memory_name, inputBuffers, inputSize, outputBuffers, outputSize);
    QNN_INF("TalkToSvc_Inference End %d.\n", result);

    // TODO: Verify the output data here. Free the data in vector.
    for (int i = 0; i < outputSize.size(); i++) {
        sprintf_s(dataPath, BUFSIZE, MODEL_DATA_PATH_OUT, i);
        std::ofstream os(dataPath, std::ofstream::binary);
        if (!os) {
            QNN_ERR("Failed to open output file for writing: %s", dataPath);
        }
        else {
            os.write(reinterpret_cast<char*>(&(*(outputBuffers[i]))), outputSize[i]);
        }
        os.close();
    }

    result = libQNNHelper.ModelDestroy(model_name, proc_name);
    QNN_INF("TalkToSvc_Destroy End.\n");
    
    libQNNHelper.DeleteShareMemory(model_memory_name);
    QNN_INF("DeleteShareMem End.\n");

    for (int i = 0; i < inputBuffers.size(); i++) {
        free(inputBuffers[i]);
    }
    inputBuffers.clear();
    inputSize.clear();

    return 0;
}


int main(int argc, char** argv) {
    /*
    QNN_LOG_LEVEL_ERROR = 1
    QNN_LOG_LEVEL_WARN = 2
    QNN_LOG_LEVEL_INFO = 3
    QNN_LOG_LEVEL_VERBOSE = 4
    QNN_LOG_LEVEL_DEBUG = 5
    */
    // SetLogLevel(1);

    if(argc > 1 && argv[1] && argv[1][0] == 's') {
        HANDLE hSvcPipeInRead = (HANDLE)std::stoull(argv[2]);
        HANDLE hSvcPipeOutWrite = (HANDLE)std::stoull(argv[3]);
        SetLogLevel(std::stoi(argv[5]));
        SetProcInfo(argv[6], std::stoull(argv[4]));
        QNN_INF("Svc App Start proc %s.\n", argv[6]);
        Print_MemInfo("Svc App Start.");
        svcprocess_run(hSvcPipeInRead, hSvcPipeOutWrite);
        Print_MemInfo("Svc App End.");
    }
    else {
        SetLogLevel(2);
        Print_MemInfo("Main App Start.");
        hostprocess_run();
        Print_MemInfo("Main App End.");
    }

    // _CrtDumpMemoryLeaks();      // TODO: Debug memory leak, output to 'debug output' window.
    // Sleep(1000);
}
