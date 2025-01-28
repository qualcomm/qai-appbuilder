//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once


#include "Utils/Utils.hpp"
#include "Lora.hpp"
#include <string>
#include <sstream>
#include <map>


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
    std::vector<LoraAdapter> Adapters ;
    bSuccess = g_LibAppBuilder.ModelInitialize(model_name.c_str(), model_path, backend_lib_path, system_lib_path, Adapters);
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
                    std::string perf_profile, const std::vector <LoraAdapter>& Adapters ) {
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
        result = libAppBuilder.ModelInitialize(model_name, model_path, backend_lib_path, system_lib_path, Adapters);
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


// Function to parse arguments
std::map<std::string, std::vector<std::string>> parse_arguments(int argc, char* argv[]) {
    std::map<std::string, std::vector<std::string>> args;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg.find("--") == 0) { // Check if it's a named argument
            if (i + 1 < argc && argv[i + 1][0] != '-') {
                args[arg].push_back(argv[++i]); // Add to the vector for the corresponding key
            }
            else {
                args[arg].emplace_back(""); // Handle flags or arguments without values
            }
        }
        else {
            throw std::invalid_argument("Invalid argument format: " + arg);
        }
    }
    return args;
}

// Function to parse binary_updates into a map
std::map<std::string, std::vector<std::string>> parse_binary_updates(
    const std::vector<std::string>& binary_update_args) {
    std::map<std::string, std::vector<std::string>> binary_updates;

    for (const auto& update : binary_update_args) {
        std::istringstream ss(update);
        std::string graph_name, path_list;

        // Extract graph name (before the comma)
        if (!std::getline(ss, graph_name, ',')) {
            throw std::invalid_argument("Invalid format for binary update (missing graph name)");
        }

        // Extract the paths (semicolon-separated after the comma)
        if (!std::getline(ss, path_list)) {
            throw std::invalid_argument("Invalid format for binary update (missing paths)");
        }

        // Split the paths by ';'
        std::vector<std::string> paths;
        std::istringstream path_stream(path_list);
        std::string path;
        while (std::getline(path_stream, path, ';')) {
            paths.push_back(path);
        }

        if (paths.empty()) {
            throw std::invalid_argument("Invalid format for binary update (missing paths)");
        }

        binary_updates[graph_name] = paths;
    }

    return binary_updates;
}




int main(int argc, char** argv) {
    if (argc > 1 && argv[1] && argv[1][0] == 's') {  // Start server.
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
        /* Command formant: QAIAppSvc.exe --log_level <int:log_level> --QNN_Libraries_Path <str:QNN_Libraries_Path> 
                                          --model_path <str:model_path> --perf_profile <str:perf_profile> --input_path <str:input_raw_path> 
                                          --input_count <int:input_count> --memory_size<int:memory_size> 
                                          --binary_updates<str:graph_name,binary_update_path_1;binary_update_path_2>
         input files are under 'input_raw_path' and the file names format are 'input_%d.raw'.  */

        try {
            // Parse command-line arguments
            auto args = parse_arguments(argc, argv);

            // Extract and validate required parameters
            int log_level = std::stoi(args["--log_level"][0]);
            std::string qnn_lib_path = args["--QNN_Libraries_Path"][0];
            std::string model_path = args["--model_path"][0];
            std::string perf_profile = args["--perf_profile"][0];
            std::string input_list_path = args["--input_path"][0];
            int input_count = std::stoi(args["--input_count"][0]);

            // Handle optional parameters
            int memory_size = 0;
            if (args.count("--memory_size")) {
                memory_size = std::stoi(args["--memory_size"][0]);
            }

            std::map<std::string, std::vector<std::string>> binary_updates;
            if (args.count("--binary_updates")) {
                binary_updates = parse_binary_updates(args["--binary_updates"]);
            }

            SetLogLevel(log_level);

            if (log_level >= 5) {
                SetProfilingLevel(2);
            }
            else if (log_level >= 3) {
                SetProfilingLevel(1);
            }

            // Creating list of adapters 
            std::vector<LoraAdapter> Adapters;
            for (const auto& update : binary_updates) {
                std::string graph_name = update.first;
                std::vector<std::string> bin_path = update.second;
                LoraAdapter Adapter(graph_name, bin_path);
                Adapters.push_back(Adapter);
            }
            

            hostprocess_run(qnn_lib_path, model_path, input_list_path, input_count, memory_size, perf_profile, Adapters);

        }
        catch (const std::exception& e) {
            std::cerr << "Error: " << e.what() << "\n";

            printf("Command formant: QAIAppSvc.exe --log_level <int:log_level> --QNN_Libraries_Path <str:QNN_Libraries_Path> --model_path <str:model_path> --perf_profile <str:perf_profile> --input_path <str:input_raw_path> --input_count <int:input_count> --memory_size<int:memory_size> --binary_updates<str:graph_name,binary_update_path_1;binary_update_path_2>\n");
            printf("'memory_size' is an option parameter, only needed while running the model in remote process.\n");
            printf("--binary_updates is an optional parameter that can be passed if you want to apply adapters to the graph. This parameter can be specified multiple times if needed.\n");
            printf("Example: --log_level 2 --QNN_Libraries_Path C:\\user\\lorav2\\qnn_assets\\2.28.2 --model_path C:\\user\\lorav2\\running_sample_app\\models_and_input\\text_encoder.serialized_qnn_2.28.bin --perf_profile burst --input_path C:\\user\\lorav2\\runnig_qai_helper\\text_encoder_inputs --input_count 2 --binary_updates text_encoder,C:\\user\\lorav2\\running_sample_app\\models_and_input\\text_encoder_Stickers_qnn_2.28.bin;C:\\user\\lorav2\\running_sample_app\\models_and_input\\text_encoder_TShirtDesignAF.bin  --memory_size 102400000\n");
            return 1;
        }

        Print_MemInfo("Main App End.");
    }

    return 0;
}
