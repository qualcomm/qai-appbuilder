//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "genie.h"
#include "genie_iface.h"
#include <filesystem>

namespace fs = std::filesystem;

void GenieLog_Callback(const GenieLog_Handle_t handle,
                       const char *fmt,
                       GenieLog_Level_t level,
                       uint64_t timestamp,
                       va_list args)
{
    auto length = std::vsnprintf(nullptr, 0, fmt, args);
    if (length < 0)
    {
        My_Log{My_Log::Level::kWarning} << "bad args and fmt in genie callback" << std::endl;
        return;
    }

    auto *buf = new char[length];
    My_Log::Level my_level;
    switch (level)
    {
        case GENIE_LOG_LEVEL_ERROR:
            my_level = My_Log::Level::kError;
            break;
        case GENIE_LOG_LEVEL_WARN:
            my_level = My_Log::Level::kWarning;
            break;
        case GENIE_LOG_LEVEL_INFO:
            my_level = My_Log::Level::kInfo;
            break;
        case GENIE_LOG_LEVEL_VERBOSE:
            my_level = My_Log::Level::kVerbose;
            break;
    }

    std::vsnprintf(buf, length, fmt, args);
    while (buf[length - 2] == '\n')
    {
        buf[length - 2] = '\0';
        length--;
    }
    My_Log{my_level}.original(true) << buf << std::endl;
    delete[] buf;
}


void GenieContext::inference_thread()
{
    while (true)
    {
        std::unique_lock<std::mutex> lock(m_request_lock);
        m_request_cond.wait(lock, [this]
        { return m_request_ready; });     // m_request_ready == true, wakeup thread; m_request_ready == false, sleep continually.
        if (m_thread_exit)
        {
            return;
        }
        auto status = interface_->GenieDialogQuery(content);
        if (GENIE_STATUS_SUCCESS != status && GENIE_STATUS_WARNING_ABORTED != status)
        {
            My_Log{} << "Failed to get response from GenieDialog.\n";
        }

        m_inference_busy = false;
        m_request_ready = false;
    }
}

bool GenieContext::Query(const std::string &prompt, const Callback callback)
{
    if (GENIE_STATUS_SUCCESS != GenieDialog_reset(m_DialogHandle))
    {
        My_Log{} << "Failed to reset Genie Dialog.\n";
        return false;
    }

    g_CurLength = 0;
    if (!interface_->set_content(prompt))
    {
        return false;
    }

    m_request_ready = true;
    m_inference_busy = true;
    m_request_cond.notify_one();   // Notify the inference thread to work.

    std::string response;
    while (m_inference_busy)
    {
        if (!m_stream_answer.empty())
        {
            std::lock_guard<std::mutex> guard(m_stream_lock);
            response = m_stream_answer;
            m_stream_answer = "";
        }

        if (response.empty())
        {
            continue;
        }

        if (!callback(response))
        {
            break;
        }

        response = "";
        std::this_thread::sleep_for(std::chrono::milliseconds(10)); // sleep 10 ms.
    }

    if (!m_stream_answer.empty())
    {
        // send remainder data.
        callback(m_stream_answer);
        m_stream_answer = "";
    }
    return true;
}

GenieContext::Content::Content(const QueryType &query_type) : query_type_{query_type}
{
    switch (query_type_.v_)
    {
        case QueryType::TextQuery:
            new(&prompt_) std::string();
            break;
        case QueryType::TokenQuery:
            // TODO
            break;
        case QueryType::EmbeddingQuery:
            new(&embeddings_) std::vector<uint8_t>();
            break;
    }

    My_Log{} << "genie query type is " << query_type_.to_string() << std::endl;
}

GenieContext::Content::~Content()
{
    switch (query_type_.v_)
    {
        case QueryType::TextQuery:
        case QueryType::TokenQuery:
            prompt_.~basic_string();
            break;
        case QueryType::EmbeddingQuery:
            embeddings_.~vector<uint8_t>();
            break;
    }
}

GenieContext::GenieContext(const IModelConfig &model_config) :
        ContextBase(model_config),
        content(model_config.get_query_type())
{
    const std::string sample_config_str = "{\n  \"sampler\" : {\n      \"version\" : 1,\n      \"temp\" : 1.2,\n      \"top-k\" : 25,\n      \"top-p\" : 0.8\n  }\n}";
    int32_t status = 0;
    auto j = ConfigFixer{model_config}.Execute();

    if (GENIE_STATUS_SUCCESS != GenieDialogConfig_createFromJson(j.dump().c_str(), &m_ConfigHandle))
    {
        throw std::runtime_error("Failed to create the Genie Dialog config.");
    }

    status = GenieLog_create(nullptr, GenieLog_Callback, My_Log::get_genie_log_level(), &m_LogHandle);
    if ((GENIE_STATUS_SUCCESS != status) || (!m_LogHandle))
    {
        throw std::runtime_error("Failed to create the Log handle.");
    }

    status = GenieDialogConfig_bindLogger(m_ConfigHandle, m_LogHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        throw std::runtime_error("Failed to bind the log handle with the dialog config.");
    }

    status = GenieProfile_create(nullptr, &m_ProfileHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        throw std::runtime_error("Failed to create the profile handle.\n");
    }

    status = GenieDialogConfig_bindProfiler(m_ConfigHandle, m_ProfileHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        throw std::runtime_error("Failed to bind the profile handle with the dialog config.\n");
    }

    if (GENIE_STATUS_SUCCESS != GenieDialog_create(m_ConfigHandle, &m_DialogHandle))
    {
        throw std::runtime_error("Failed to create the Genie Dialog\n");
    }

    status = GenieSamplerConfig_createFromJson(sample_config_str.c_str(), &m_SamplerConfigHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        throw std::runtime_error("Failed to create sampler config.\n");
    }

    status = GenieDialog_getSampler(m_DialogHandle, &m_SamplerHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        throw std::runtime_error("Failed to get sampler.\n");
    }

    if (!m_stream_thread)
    {
        m_stream_thread = std::make_unique<std::thread>(&GenieContext::inference_thread, this);
    }
    interface_ = Interface::CreateInterface(this);
}

GenieContext::~GenieContext()
{
#ifdef GENIE_BUILDER_DEBUG
    My_Log{} << "GenieContext::~GenieContext():\n";
#endif

    int32_t status = 0;

    // Notify thread exiting.
    if (m_stream_thread)
    {
        m_thread_exit = true;
        m_request_ready = true;
        m_request_cond.notify_one();
    }

    if (m_ConfigHandle != nullptr)
    {
        if (GENIE_STATUS_SUCCESS != GenieDialogConfig_free(m_ConfigHandle))
        {
            My_Log{} << "Failed to free the Genie Dialog config.\n";
        }
    }

    if (m_DialogHandle != nullptr)
    {
        if (GENIE_STATUS_SUCCESS != GenieDialog_free(m_DialogHandle))
        {
            My_Log{} << "Failed to free the Genie Dialog.\n";
        }
    }

    status = GenieSamplerConfig_free(m_SamplerConfigHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        My_Log{} << "Failed to free the sampler config." << std::endl;
    }

    status = GenieLog_free(m_LogHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        My_Log{} << "Failed to free the Log handle." << std::endl;
    }

    status = GenieProfile_free(m_ProfileHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        My_Log{} << "Failed to free the profile handle." << std::endl;
    }

    // Waiting thread clean.
    if (m_stream_thread)
    {
        m_stream_thread->join();
        m_stream_thread = nullptr;

        // reset the global variable.
        m_request_ready = false;
        m_thread_exit = false;
    }

    delete interface_;
    interface_ = nullptr;
    My_Log{} << "GenieContext::~GenieContext() Done:\n";
}

bool GenieContext::Stop()
{
    if (GENIE_STATUS_SUCCESS != GenieDialog_signal(m_DialogHandle, GENIE_DIALOG_ACTION_ABORT))
    {
        My_Log{} << "Failed to stop generation.\n";
        return false;
    }

    return true;
}

bool GenieContext::SetParams(const std::string max_length,
                             const std::string temp,
                             const std::string top_k,
                             const std::string top_p)
{
    int32_t status = 0;

    g_MaxLength = std::stoi(max_length);

    status = GenieSamplerConfig_setParam(m_SamplerConfigHandle, "temp", temp.c_str());
    if (GENIE_STATUS_SUCCESS != status)
    {
        My_Log{} << "Failed to setParam.\n";
        return false;
    }

    status = GenieSamplerConfig_setParam(m_SamplerConfigHandle, "top-k", top_k.c_str());
    if (GENIE_STATUS_SUCCESS != status)
    {
        My_Log{} << "Failed to setParam.\n";
        return false;
    }

    status = GenieSamplerConfig_setParam(m_SamplerConfigHandle, "top-p", top_p.c_str());
    if (GENIE_STATUS_SUCCESS != status)
    {
        My_Log{} << "Failed to setParam.\n";
        return false;
    }

    status = GenieSamplerConfig_setParam(m_SamplerConfigHandle, "type", "basic");
    if (GENIE_STATUS_SUCCESS != status)
    {
        My_Log{} << "Failed to setParam type.\n";
        return false;
    }

    status = GenieSampler_applyConfig(m_SamplerHandle, m_SamplerConfigHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        My_Log{My_Log::Level::kError} << "Failed to apply sampler config.\n";
        return false;
    }

    return true;
}

bool GenieContext::SetStopSequence(const std::string &stop_sequences)
{
    if (GENIE_STATUS_SUCCESS != GenieDialog_setStopSequence(m_DialogHandle, stop_sequences.c_str()))
    {
        My_Log{} << "Failed to set stop sequence.\n";
        return false;
    }

    return true;
}

void MyAllocCallback(const size_t size, const char **allocatedData)
{
    *allocatedData = reinterpret_cast<const char *>(malloc(size));
}

size_t GenieContext::TokenLength(const std::string &text)
{
    GenieTokenizer_Handle_t tokenizerHandle = nullptr;
    Genie_Status_t status = GenieDialog_getTokenizer(m_DialogHandle, &tokenizerHandle);
    if (status != GENIE_STATUS_SUCCESS)
    {
        My_Log{}.original(true) << "\n";
        My_Log{My_Log::Level::kError} << "获取 tokenizer 失败，错误码: " << status << std::endl;
        return 0;
    }

    const int32_t *tokenIds = nullptr;
    uint32_t numTokenIds = 0;

    status = GenieTokenizer_encode(tokenizerHandle, text.c_str(), MyAllocCallback, &tokenIds, &numTokenIds);

    if (status != GENIE_STATUS_SUCCESS)
    {
        My_Log{}.original(true) << "\n";
        My_Log{My_Log::Level::kError} << "encode failed: : " << status << ", "
                                      << "the string length is: " << text.size() << std::endl;
        return text.size();
    }

    free((void *) tokenIds);
    return static_cast<size_t>(numTokenIds);
}

void GenieContext::applyLora(const std::string engineRole, const std::string loraAdapterName)
{
    int32_t status = GenieDialog_applyLora(m_DialogHandle, engineRole.c_str(), loraAdapterName.c_str());
    if (GENIE_STATUS_SUCCESS != status)
    {
        throw std::runtime_error("Failed to apply the LoRA adapter.");
    }
}

void GenieContext::setLoraStrength(const std::string engineRole,
                                   const std::unordered_map<std::string, float> &alphaValue)
{
    for (auto it = alphaValue.begin(); it != alphaValue.end(); it++)
    {
        int32_t status = GenieDialog_setLoraStrength(m_DialogHandle, engineRole.c_str(), it->first.c_str(), it->second);
        if (GENIE_STATUS_SUCCESS != status)
        {
            throw std::runtime_error("Failed to set the LoRA alpha strength.");
        }
    }
}

json GenieContext::HandleProfile()
{
    const Genie_AllocCallback_t callback([](size_t size, const char **data)
                                         {
                                             *data = (char *) malloc(size);
                                             if (*data == nullptr)
                                             {
                                                 My_Log{} << "cannot allocate memory for JSON data.\n";
                                             }
                                         });

    const char *jsonData = nullptr;
    const Genie_Status_t status = GenieProfile_getJsonData(m_ProfileHandle, callback, &jsonData);
    if (GENIE_STATUS_SUCCESS != status)
    {
        My_Log{My_Log::Level::kError} << "get the profile data failed: " << status << "\n";
        return "";
    }

    std::string jsonStr(jsonData);
    free((char *) jsonData);

    json result;
    try
    {
        json j = json::parse(jsonStr);
        if (!j["components"].empty() && j["components"][0]["type"] == "dialog")
        {
            const auto &events = j["components"][0]["events"];
            if (!events.empty())
            {
                const auto &last_event = events.back();
                if (last_event.at("type") == "GenieDialog_query")
                {
                    std::ostringstream oss;

                    // time_to_first_token
                    oss << std::fixed << std::setprecision(2)
                        << last_event.at("time-to-first-token")["value"].get<double>() / 1000.0;
                    result["time_to_first_token"] = oss.str();

                    // token_generation_time
                    oss.str("");
                    oss << std::fixed << std::setprecision(2)
                        << last_event.at("token-generation-time")["value"].get<double>() / 1000.0;
                    result["token_generation_time"] = oss.str();

                    // prompt_processing_rate
                    oss.str("");
                    oss << std::fixed << std::setprecision(2)
                        << last_event.at("prompt-processing-rate")["value"].get<double>();
                    result["prompt_processing_rate"] = oss.str();

                    // token_generation_rate
                    oss.str("");
                    oss << std::fixed << std::setprecision(2)
                        << last_event.at("token-generation-rate")["value"].get<double>();
                    result["token_generation_rate"] = oss.str();

                    // integer values
                    result["num_prompt_tokens"] = last_event.at("num-prompt-tokens")["value"];
                    result["num_generated_tokens"] = last_event.at("num-generated-tokens")["value"];
                }
            }
        }
    }
    catch (std::exception &e)
    {
        My_Log{My_Log::Level::kError} << "parse profile failed: " << jsonStr << "\n";
        return "";
    }
    return result;
}

struct GenieContext::ConfigFixer::FixedInfo
{
    const char *item_str_{};
    json::json_pointer jp_;
    enum CheckType
    {
        kString,
        kArrayString
    } check_type_;
    bool optional_{};
    bool is_forcast_dir_{false};
};

json GenieContext::ConfigFixer::Execute()
{
    std::ifstream file(model_config_.get_config_path());
    json j;
    file >> j;

    /* @formatter:off */
    std::vector<FixedInfo> fixed_items{
            {"tokenizer", json::json_pointer("/dialog/tokenizer/path"), FixedInfo::kString, false},
            {"extensions", json::json_pointer("/dialog/engine/backend/extensions"), FixedInfo::kString, true},
            {"ctx-bins", json::json_pointer("/dialog/engine/model/binary/ctx-bins"), FixedInfo::kArrayString, false},
            {"forecast", json::json_pointer("/dialog/ssd-q1/forecast-prefix-name"), FixedInfo::kArrayString, true, true},
    };

/* @formatter:on */

    for (auto &fixed_item: fixed_items)
    {
        if (!FixedPath(j, fixed_item))
        {
            throw std::runtime_error("fixed the config path failed");
        }
    }

    if (Genie_getApiMinorVersion() >= 11)
    {
        auto jp = json::json_pointer("/dialog/engine/backend/QnnHtp");
        j.at(jp)["use-mmap"] = true;
        j.at(jp)["allow-async-init"] = true;
    }
    My_Log{} << "fixed the config path successfully\n";
    return j;
}

bool GenieContext::ConfigFixer::FixedPath(json &j, ConfigFixer::FixedInfo &info)
{
    static auto executable_path = fs::path{__argv[0]}.is_absolute() ?
                                  fs::path{__argv[0]}.parent_path().generic_string() :
                                  fs::path{fs::current_path().generic_string() + "/" + __argv[0]}
                                          .parent_path()
                                          .generic_string();

    std::string file_path;
    auto get_fixed_path{[&](const json &j) -> bool
                        {
                            bool rs{false};
                            std::string file_name;

                            if (!j.is_string())
                            {
                                My_Log{} << info.item_str_ << " json object is not string\n";
                                goto done;
                            }

                            if (info.is_forcast_dir_)
                            {
                                file_path = model_config_.get_model_path();
                                rs = true;
                                goto done;
                            }

                            file_name = fs::path{j.get<std::string>()}.filename().generic_string();
                            if (file_name.empty())
                            {
                                My_Log{} << info.item_str_ << " json object file name is not empty\n";
                                goto done;
                            }

                            file_path = model_config_.get_model_path() + "/" + file_name;
                            if (File::IsFileExist(file_path))
                            {
                                rs = true;
                                goto done;
                            }

                            My_Log{} << "file path: " << file_path << " is not exist\n";

                            file_path = executable_path + "/" + file_name;
                            if (!File::IsFileExist(file_path))
                            {
                                My_Log{} << "file path: " << file_path << " is not exist\n";
                                goto done;
                            }

                            rs = true;
                            done:
                            return rs;
                        }};

    if (!j.contains(info.jp_))
    {
        My_Log{} << info.item_str_ << " json object is not found\n";
        if (!info.optional_)
        {
            return false;
        }
        return true;
    }

    switch (info.check_type_)
    {
        case FixedInfo::kString:
            if (get_fixed_path(j.at(info.jp_)))
            {
                j.at(info.jp_) = file_path;
                return true;
            }
            if (info.optional_)
                j.at(info.jp_.parent_pointer()).erase(info.jp_.back());
            else
                return false;
            break;
        case FixedInfo::kArrayString:
            for (auto &item: j.at(info.jp_))
            {
                if (!get_fixed_path(item) && !info.optional_)
                {
                    return false;
                }
                item = file_path;
            }
            break;
    }
    return true;
}
