//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once


#include "Utils/Utils.hpp"


// ============================== Service / QAIAppSvc ============================== //

#define ACTION_OK        "OK"
#define ACTION_FAILED    "Failed"

LibAppBuilder g_LibAppBuilder;

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

    Print_MemInfo("ModelLoad::ModelInitialize Start.");
    QNN_INF("ModelLoad::ModelInitialize::Model name %s\n", model_name.c_str());
    bSuccess = g_LibAppBuilder.ModelInitialize(model_name.c_str(), model_path, backend_lib_path, system_lib_path);
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
    std::string strBufferArray    = commands[3];
    std::string perfProfile       = commands[4];


    // Open share memory and read the inference data from share memory.
    LPVOID lpBase = OpenShareMem(share_memory_name, share_memory_size);

    std::vector<uint8_t*> inputBuffers;
    std::vector<size_t> inputSize;
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;
    outputSize.push_back(12345);

    // Fill data from 'pShareMemInfo->lpBase' to 'inputBuffers' vector before inference the model.
    ShareMemToVector(strBufferArray, (uint8_t*)lpBase, inputBuffers, inputSize);

    Print_MemInfo("ModelRun::ModelInference Start.");
    //QNN_INF("ModelRun::ModelInference %s\n", model_name.c_str());
    bSuccess = g_LibAppBuilder.ModelInference(model_name.c_str(), inputBuffers, outputBuffers, outputSize, perfProfile);
    //QNN_INF("ModelRun::ModelInference End ret = %d\n", bSuccess);
    Print_MemInfo("ModelRun::ModelInference End.");

    // Fill data from outputBuffers to 'pShareMemInfo->lpBase' and send back to client.
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
    bSuccess = g_LibAppBuilder.ModelDestroy(model_name.c_str());
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
            QNN_WAR("Svc::Failed to read from hSvcPipeInRead, perhaps parent process closed pipe or died.\n");
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


// ============================== Client / QAIAppSvc ============================== //
#define BUFSIZE             (256)

// test code, load and run model.
int hostprocess_run(std::string qnn_lib_path, std::string model_path, 
                    std::string input_raw_path, int input_count, int memory_size,
                    std::string perf_profile) {
    BOOL result = false;

    std::string MODEL_NAME = "<model_name>";
    std::string PROC_NAME = "<proc_name>";

    std::string model_memory_name = MODEL_NAME;
    std::string model_name = MODEL_NAME;
    std::string proc_name = PROC_NAME;

    std::string backend_lib_path = qnn_lib_path + "\\QnnHtp.dll";
    std::string system_lib_path = qnn_lib_path + "\\QnnSystem.dll";

    std::string input_data_path = input_raw_path + "\\input_%d.raw";
    std::string output_data_path = input_raw_path + "\\output_%d.raw";

    QNN_INF("Load data from raw data file to vector Start.\n");
    std::vector<uint8_t*> inputBuffers;
    std::vector<size_t> inputSize;
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;
    char dataPath[BUFSIZE];

    for (int i = 0; i < input_count; i++) {
        sprintf_s(dataPath, BUFSIZE, input_data_path.c_str(), i);
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

    LibAppBuilder libAppBuilder;

    if (0 == memory_size) {    // Load & run model locally.
        QNN_INF("Load and run model locally Start.\n");
        result = libAppBuilder.ModelInitialize(model_name, model_path, backend_lib_path, system_lib_path);
        Print_MemInfo("ModelInitialize End.");

        // SetPerfProfileGlobal("burst");

        {
            // Inference.
            result = libAppBuilder.ModelInference(model_name, inputBuffers, outputBuffers, outputSize, perf_profile);
            
            // Verify the output data here. Free the data in vector.
            for (int i = 0; i < outputSize.size(); i++) {
                sprintf_s(dataPath, BUFSIZE, output_data_path.c_str(), i);
                std::ofstream os(dataPath, std::ofstream::binary);
                if (!os) {
                    QNN_ERR("Failed to open output file for writing: %s", dataPath);
                }
                else {
                    os.write(reinterpret_cast<char*>(&(*(outputBuffers[i]))), outputSize[i]);
                }
                os.close();
            }
            
            for (int i = 0; i < outputBuffers.size(); i++) {
                free(outputBuffers[i]);
            }
            outputBuffers.clear();
            outputSize.clear();
            Print_MemInfo("ModelInference End.");
        }

        result = libAppBuilder.ModelDestroy(model_name);
        
        QNN_INF("Load and run model locally End.\n");

        Print_MemInfo("Load and run model locally End.");
    }
    else {    // Load & run model in remote process.
        libAppBuilder.CreateShareMemory(model_memory_name, memory_size);

        // Add 'TalkToSvc_*' function to 'libAppBuilder'.
        QNN_INF("TalkToSvc_Initialize Start.\n");
        result = libAppBuilder.ModelInitialize(model_name, proc_name, model_path, backend_lib_path, system_lib_path);
        QNN_INF("TalkToSvc_Initialize End %d.\n", result);

        QNN_INF("TalkToSvc_Inference Start.\n");
        result = libAppBuilder.ModelInference(model_name, proc_name, model_memory_name, inputBuffers, inputSize, outputBuffers, outputSize, perf_profile);
        QNN_INF("TalkToSvc_Inference End %d.\n", result);

        // Verify the output data here. Free the data in vector.
        for (int i = 0; i < outputSize.size(); i++) {
            sprintf_s(dataPath, BUFSIZE, output_data_path.c_str(), i);
            std::ofstream os(dataPath, std::ofstream::binary);
            if (!os) {
                QNN_ERR("Failed to open output file for writing: %s", dataPath);
            }
            else {
                os.write(reinterpret_cast<char*>(&(*(outputBuffers[i]))), outputSize[i]);
            }
            os.close();
        }

        result = libAppBuilder.ModelDestroy(model_name, proc_name);
        QNN_INF("TalkToSvc_Destroy End.\n");

        libAppBuilder.DeleteShareMemory(model_memory_name);
        QNN_INF("DeleteShareMem End.\n");

        // outputBuffers is in ShareMemory, so we don't need to free this memory.
    }

    // Release input buffer.
    for (int i = 0; i < inputBuffers.size(); i++) {
        free(inputBuffers[i]);
    }
    inputBuffers.clear();
    inputSize.clear();

    return 0;
}


int main(int argc, char** argv) {

    if(argc > 1 && argv[1] && argv[1][0] == 's') {  // Start server.
        HANDLE hSvcPipeInRead = (HANDLE)std::stoull(argv[2]);
        HANDLE hSvcPipeOutWrite = (HANDLE)std::stoull(argv[3]);
        SetLogLevel(std::stoi(argv[5]));
        SetProfilingLevel(std::stoi(argv[6]));
        SetProcInfo(argv[7], std::stoull(argv[4]));
        QNN_INF("Svc App Start proc %s.\n", argv[7]);
        Print_MemInfo("Svc App Start.");
        svcprocess_run(hSvcPipeInRead, hSvcPipeOutWrite);
        Print_MemInfo("Svc App End.");
    }
    else {  // Start test mode to load & run model.
        // QAIAppSvc.exe <1:int:log_level> <2:str:QNN_Libraries_Path> <3:str:model_path> <4:str:perf_profile> 
        //                  <5:str:input_raw_path> <6:int:input_count> <7:int:memory_size>
        // input files are under 'input_raw_path' and the file names format are 'input_%d.raw'.
        // QAIAppSvc.exe 5 ""

        if (argc <= 6) {
            printf("Command formant: QAIAppSvc.exe <1:int:log_level> <2:str:QNN_Libraries_Path> <3:str:model_path> <4:str:perf_profile> <5:str:input_raw_path> <6:int:input_count> <7:int:memory_size>\n");
            printf("'memory_size' is an option parameter, only needed while running the model in remote process.\n");
            printf("Example: QAIAppSvc.exe 5 \"C:\\test_model\\QNN_binaries\" \"C:\\test_model\\qnn_model\\InceptionV3_quantized.serialized.v73.bin\" \"bust\" \"C:\\test_model\\input\" 1 102400000");
            return 0;
        }

        int log_level = std::stoi(argv[1]);
        char* qnn_lib_path = argv[2];
        char* model_path = argv[3];
        char* perf_profile = argv[4];
        char* input_list_path = argv[5];
        int input_count = std::stoi(argv[6]);
        int memory_size = 0;
        if (argc > 7) {
            memory_size = std::stoi(argv[7]);
        }

        SetLogLevel(log_level);

        if(log_level >= 5) {
            SetProfilingLevel(2);
        }
        else if(log_level >= 3) {
            SetProfilingLevel(1);
        }
        
        hostprocess_run(qnn_lib_path, model_path, input_list_path, input_count, memory_size, perf_profile);

        Print_MemInfo("Main App End.");
    }

    return 0;
}

