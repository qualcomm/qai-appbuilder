//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef CONFIG_H
#define CONFIG_H

#include "log.h"
#include "utils.h"
#include "model/model_config.h"
#include <CLI/CLI.hpp>
#include <fstream>
#include <GenieCommon.h>

namespace fs = std::filesystem;
#ifdef WIN32

#include <direct.h>

#endif

class Config
{
public:
    Config(int argc, char *argv[]) : argc_{argc}, argv_{argv}
    {}

    bool Process();

    IModelConfig get_mode_manager_config()
    { return model_config_; }

    bool NeedLoadModel() const
    { return loadModel; }

    int get_port() const
    { return port_; }

private:
    int argc_;
    char **argv_;
    int port_ = 8910;
    bool loadModel = false;
    IModelConfig model_config_;
};

bool Config::Process()
{
    int logLevel{My_Log::Level::kWarning};
    std::string log_path, &config_file{model_config_.config_file_};
    bool version{false};

    CLI::App app{"Genie API Service - Powerful Local LLM Service"};
    app.footer("\nSupport: https://github.com/quic/ai-engine-direct-helper");

    app.add_option("-c,--config_file", config_file, "Path to the config file.")->required(true);
    app.add_option("--adapter", model_config_.loraAdapter, "the adapter of lora");

    app.add_flag("-l,--load_model", loadModel, "Load the model.");
    app.add_flag("-a,--all_text", model_config_.outputAllText, "Output all text includes tool calls text.");
    app.add_flag("-t,--enable_thinking", model_config_.enableThinking, "Enable thinking mode.");
    app.add_flag("-v,--version", version, "Print version info and exit.");

    app.add_option("-n,--num_response", model_config_.numResponse,
                   "The number of rounds saved in the historical record");
    app.add_option("-o,--min_output_num", model_config_.minOutputNum, "The minimum number of tokens output");
    app.add_option("-d,--loglevel", logLevel,
                   "log level setting for record. 1: Error, 2: Warning, 3: Info, 4: Debug, 5: Verbose");
    app.add_option("-f,--logfile", log_path, "log file path, it's a option");
    app.add_option("--lora_alpha", model_config_.loraAlpha, "lora Alpha Value");
    app.add_option("-p,--port", port_, "Port used for running");

    try
    {
        app.parse(argc_, argv_);
        My_Log::Init(static_cast<My_Log::Level>(logLevel), std::move(log_path));
    }
    catch (const CLI::CallForHelp &e)
    {
        My_Log{My_Log::Level::kAlways} << app.help() << std::endl;
        return false;
    }
    catch (const std::exception &e)
    {
        My_Log{My_Log::Level::kAlways} << e.what() << std::endl;
        return false;
    }

    My_Log{}.original(true) << "GenieAPIService: "
                            << QAI_APP_BUILDER_MAJOR_VERSION << "."
                            << QAI_APP_BUILDER_MINOR_VERSION << "."
                            << QAI_APP_BUILDER_PATCH_VERSION << ", "
                            << "Genie Library: "
                            << Genie_getApiMajorVersion() << "."
                            << Genie_getApiMinorVersion() << "."
                            << Genie_getApiPatchVersion() << std::endl;

    if (version)
    {
        return false;
    }

    char buffer[1024];
    if (!getcwd(buffer, sizeof(buffer)))
    {
        My_Log{} << "get current dir failed" << std::endl;
        return false;
    }

    CurrentDir = fs::path{buffer}.generic_string();
    My_Log{} << "current work dir: " << CurrentDir << std::endl;

    RootDir = fs::path{argv_[0]}.is_absolute() ?
              fs::path{argv_[0]}.parent_path().generic_string() :
              fs::path{CurrentDir + "/" + argv_[0]}
                      .parent_path()
                      .generic_string();
    My_Log{} << "root dir: " << RootDir << std::endl;
    My_Log::ShowStatus();

    if (!File::IsFileExist(config_file))
    {
        My_Log{My_Log::Level::kError} << "config file is not found: " << config_file << std::endl;
        return false;
    }
    std::replace(config_file.begin(), config_file.end(), '\\', '/');

    if (!isPortAvailable(port_))
    {
        My_Log{} << RED << "service already exist." << RESET << std::endl;
        return false;
    }

    return true;
}

#endif //CONFIG_H
