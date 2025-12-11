#ifndef GENIEAPICLIENT_SLN_CONFIG_H
#define GENIEAPICLIENT_SLN_CONFIG_H

#include "core/log.h"
#include "core/utils.h"
#include "core/model/model_config.h"
#include <fstream>
#include <CLI/CLI.hpp>

class Config
{
public:
    Config(int argc, char *argv[]) : argc_{argc}, argv_{argv}
    {
#ifdef __ANDROID__
        __argc = argc;
        __argv = argv;
#endif
    }

    bool Process();

    IModelConfig get_mode_manager_config()
    {
        return model_config_;
    }

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
    std::string log_path, embedded_file, &config_file{model_config_.config_file_};
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

    app.add_option("-T,--embedding_table", embedded_file,
                   "Token-to-Embedding lookup table provided as a file");

    try
    {
        app.parse(argc_, argv_);
        My_Log::Init(static_cast<My_Log::Level>(logLevel), std::move(log_path));
    }
    catch (const CLI::CallForHelp &e)
    {
        std::cout << app.help() << std::endl;
        return false;
    }
    catch (const CLI::ParseError &e)
    {
        std::cout << e.what();
        return false;
    }
    catch (const std::exception &e)
    {
        std::cout << e.what() << std::endl;
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

    My_Log::ShowStatus();

    if (!File::IsFileExist(config_file) || File::IsFileEmpty(embedded_file))
    {
        My_Log{My_Log::Level::kError} << "Config file not found: " << config_file << std::endl;
        return false;
    }

    std::replace(config_file.begin(), config_file.end(), '\\', '/');

    auto &embedded_buffer{model_config_.embedded_buffer_};

    if (!embedded_file.empty())
    {
        if (!File::IsFileExist(embedded_file) ||
            File::IsFileEmpty(embedded_file))
        {
            My_Log{} << "the query type is embedded_token_path is not a correct file\n";
            return false;
        }

        model_config_.query_type_.v_ = QueryType::EmbeddingQuery;
        std::ifstream in(embedded_file, std::ifstream::binary);
        auto file_size = File::get_file_size(embedded_file);
        // std::make_unique<uint8_t[]>(file_size);
        embedded_buffer.resize(file_size);
        in.read(reinterpret_cast<char *>(embedded_buffer.data()), file_size);
    }

    if (!isPortAvailable(port_))
    {
        My_Log{} << RED << "Service already exist." << RESET << std::endl;
        return false;
    }

    return true;
}

#endif //GENIEAPICLIENT_SLN_CONFIG_H
