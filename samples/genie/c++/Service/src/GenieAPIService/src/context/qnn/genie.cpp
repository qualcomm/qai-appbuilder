//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "genie.h"
#include "genie_interface.h"
#include "utils.h"
#include "log.h"
#include "config_fixer.h"

void GenieLog_Callback(const GenieLog_Handle_t  /*handle*/,
                       const char *fmt,
                       GenieLog_Level_t level,
                       uint64_t /*timestamp*/,
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
        m_request_cond.wait(lock,
                            [this] { return m_request_ready; });     // m_request_ready == true, wakeup thread; m_request_ready == false, sleep continually.
        if (m_thread_exit)
        {
            return;
        }

        auto status = inf_impl_->inf_->GenieDialogQuery();
        if (GENIE_STATUS_SUCCESS != status && GENIE_STATUS_WARNING_ABORTED != status)
        {
            inference_succeed_ = false;
            My_Log{My_Log::Level::kError} << "Failed to get response from GenieDialog.\n";
        }
        else
        {
            inference_succeed_ = true;
        }

        m_inference_busy = false;
        m_request_ready = false;
    }
}

bool GenieContext::Query(const ModelInput &model_input, const Callback &callback)
{
    if (GENIE_STATUS_SUCCESS != GenieDialog_reset(m_DialogHandle))
    {
        My_Log{} << "Failed to reset Genie Dialog.\n";
        return false;
    }

    inf_impl_->inf_->cur_length_ = 0;
    if (!inf_impl_->inf_->set_content(const_cast<ModelInput &>(model_input)))
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
            m_stream_answer.clear();
        }

        if (response.empty())
        {
            continue;
        }

        if (!callback(response))
        {
            break;
        }

        response.clear();
        std::this_thread::sleep_for(std::chrono::milliseconds(10)); // sleep 10 ms.
    }

    if (!inference_succeed_)
    {
        return false;
    }

    if (!m_stream_answer.empty())
    {
        // send remainder data.
        callback(m_stream_answer);
        m_stream_answer.clear();
    }
    return true;
}

GenieContext::GenieContext(const IModelConfig &model_config) :
        ContextBase(model_config)
{
    const std::string sample_config_str = "{\n  \"sampler\" : {\n      \"version\" : 1,\n      \"temp\" : 1.2,\n      \"top-k\" : 25,\n      \"top-p\" : 0.8\n  }\n}";
    int32_t status = 0;
    auto j = ConfigFixer{model_config}.Execute();

    if (GENIE_STATUS_SUCCESS != GenieDialogConfig_createFromJson(j.dump().c_str(), &m_ConfigHandle))
    {
        throw std::runtime_error("Failed to create the Genie Dialog config.");
    }

    status = GenieLog_create(nullptr, GenieLog_Callback, get_genie_log_level(), &m_LogHandle);
    if ((GENIE_STATUS_SUCCESS != status) || (!m_LogHandle))
    {
        throw std::runtime_error("Failed to create the Log handle.");
    }

    status = GenieDialogConfig_bindLogger(m_ConfigHandle, m_LogHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        throw std::runtime_error("Failed to bind the log handle with the dialog config");
    }

    status = GenieProfile_create(nullptr, &m_ProfileHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        throw std::runtime_error("Failed to create the profile handle");
    }

    status = GenieDialogConfig_bindProfiler(m_ConfigHandle, m_ProfileHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        throw std::runtime_error("Failed to bind the profile handle with the dialog config");
    }

    if (GENIE_STATUS_SUCCESS != GenieDialog_create(m_ConfigHandle, &m_DialogHandle))
    {
        throw std::runtime_error("Failed to create the Genie Dialog");
    }

    status = GenieSamplerConfig_createFromJson(sample_config_str.c_str(), &m_SamplerConfigHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        throw std::runtime_error("Failed to create sampler config");
    }

    status = GenieDialog_getSampler(m_DialogHandle, &m_SamplerHandle);
    if (GENIE_STATUS_SUCCESS != status)
    {
        throw std::runtime_error("Failed to get sampler");
    }

    if (!inf_impl_)
    {
        inf_impl_ = new QInterfaceImpl{this};
    }

    if (!m_stream_thread)
    {
        m_stream_thread = std::make_unique<std::thread>(&GenieContext::inference_thread, this);
    }
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

    delete inf_impl_;
    inf_impl_ = nullptr;
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

bool GenieContext::SetParams(const std::string &max_length,
                             const std::string &temp,
                             const std::string &top_k,
                             const std::string &top_p)
{
    int32_t status = 0;
    inf_impl_->inf_->max_length_ = std::stoi(max_length);
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

bool GenieContext::GenerateTextToken(const std::string &text, const int32_t *&buf, uint32_t &len)
{
    GenieTokenizer_Handle_t tokenizerHandle = nullptr;
    Genie_Status_t status = GenieDialog_getTokenizer(m_DialogHandle, &tokenizerHandle);
    if (status != GENIE_STATUS_SUCCESS)
    {
        My_Log{}.original(true) << "\n";
        My_Log{My_Log::Level::kError} << "get tokenizer failed: " << status << std::endl;
        return false;
    }

    status = GenieTokenizer_encode(tokenizerHandle, text.c_str(),
                                   [](const size_t size, const char **allocatedData)
                                   {
                                       *allocatedData = reinterpret_cast<const char *>(malloc(size));
                                   },
                                   &buf,
                                   &len);

    if (status != GENIE_STATUS_SUCCESS)
    {
        My_Log{}.original(true) << "\n";
        My_Log{My_Log::Level::kError} << "encode failed: " << status << ", "
                                      << "the string length is: " << text.size() << std::endl;
        return false;
    }
    return true;
}

size_t GenieContext::TokenLength(const std::string &text)
{
    const int32_t *buf;
    uint32_t len;
    if (!GenerateTextToken(text, buf, len))
    {
        return text.size();
    }

    free((void *) buf);
    return len;
}

void GenieContext::applyLora(const std::string &engineRole, const std::string &loraAdapterName)
{
    int32_t status = GenieDialog_applyLora(m_DialogHandle, engineRole.c_str(), loraAdapterName.c_str());
    if (GENIE_STATUS_SUCCESS != status)
    {
        throw std::runtime_error("Failed to apply the LoRA adapter.");
    }
}

void GenieContext::setLoraStrength(const std::string &engineRole,
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

GenieLog_Level_t GenieContext::get_genie_log_level()
{
    switch (My_Log::Level_)
    {
        case My_Log::Level::kError:
            return GENIE_LOG_LEVEL_ERROR;
        case My_Log::Level::kWarning:
            return GENIE_LOG_LEVEL_WARN;
        case My_Log::Level::kInfo:
            return GENIE_LOG_LEVEL_INFO;
        default:
            return GENIE_LOG_LEVEL_VERBOSE;
    }
}
