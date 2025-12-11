//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "mnn.h"

#include <llm/llm.hpp>
#include <MNN/AutoTime.hpp>
#include <MNN/expr/ExecutorScope.hpp>

#include "core/log.h"
#include "core/utils.h"

#define MNN_BUILDER_DEBUG 1

using namespace MNN::Transformer;

class MNNContext::Impl
{
public:
    std::unique_ptr<Llm> m_llm;
};

// https://github.com/alibaba/MNN/blob/master/apps/Android/MnnLlmChat/app/src/main/cpp/utf8_stream_processor.hpp
class Utf8StreamProcessor
{
public:
    explicit Utf8StreamProcessor(std::function<void(std::string &)> callback)
            : callback(std::move(callback))
    {}

    void processStream(const char *str, size_t len)
    {
        utf8Buffer.append(str, len);

        size_t i = 0;
        std::string completeChars;
        while (i < utf8Buffer.size())
        {
            int length = utf8CharLength(static_cast<unsigned char>(utf8Buffer[i]));
            if (length == 0 || i + length > utf8Buffer.size())
            {
                break;
            }
            completeChars.append(utf8Buffer, i, length);
            i += length;
        }
        utf8Buffer = utf8Buffer.substr(i);
        if (!completeChars.empty())
        {
            callback(completeChars);
        }
    }

    static int utf8CharLength(unsigned char byte)
    {
        if ((byte & 0x80) == 0) return 1;
        if ((byte & 0xE0) == 0xC0) return 2;
        if ((byte & 0xF0) == 0xE0) return 3;
        if ((byte & 0xF8) == 0xF0) return 4;
        return 0;
    }

private:
    std::string utf8Buffer;
    std::function<void(std::string &)> callback;
};

// https://github.com/alibaba/MNN/blob/master/apps/Android/MnnLlmChat/app/src/main/cpp/llm_stream_buffer.hpp
class LlmStreamBuffer : public std::streambuf
{
public:
    using CallBack = std::function<void(const char *str, size_t len)>;;

    explicit LlmStreamBuffer(CallBack callback) :
            m_callback(std::move(callback))
    {}

protected:
    std::streamsize xsputn(const char *s, std::streamsize n)

    override
    {
        if (m_callback)
        {
            m_callback(s, n);
        }
        return n;
    }

private:
    CallBack m_callback = nullptr;
};

MNNContext::MNNContext(const IModelConfig &info) :
        ContextBase(info)
{
    impl_ = new Impl{};
    MNN::BackendConfig backendConfig;
    auto executor = MNN::Express::Executor::newExecutor(MNN_FORWARD_CPU, backendConfig, 1);
    MNN::Express::ExecutorScope s(executor);

    impl_->m_llm = std::unique_ptr<Llm>(Llm::createLLM(const_cast<IModelConfig &>(info).get_config_path()));
    impl_->m_llm->set_config("{\"tmp_path\":\"tmp\"}");

    bool result = impl_->m_llm->load();
    if (!result)
    {
        m_isLoaded = false;
        throw std::runtime_error("MNNContext::MNNContext Load model failed.\n");
    }
    m_isLoaded = true;
}

MNNContext::~MNNContext()
{
    impl_->m_llm = nullptr;
    delete impl_;
    My_Log{} << "MNNContext::~MNNContext() Done。\n";
}

// TODO: run the query in thread to speed up the performance.
// https://github.com/alibaba/MNN/blob/master/transformers/llm/engine/demo/llm_demo.cpp
// https://github.com/alibaba/MNN/blob/master/transformers/llm/engine/include/llm/llm.hpp
// https://github.com/alibaba/MNN/blob/master/transformers/llm/engine/src/llm.cpp
bool MNNContext::Query(const std::string &prompt, const Callback callback)
{
#ifdef MNN_BUILDER_DEBUG
    My_Log{} << "\n[Prompt]:\n"
             << prompt << "\n------------\n\n"
             << "[Response]:\n";
#endif

    m_stop = false;
    impl_->m_llm->reset();

    std::vector<int> input_ids = impl_->m_llm->tokenizer_encode(prompt);
    auto context = impl_->m_llm->getContext();

    Utf8StreamProcessor processor([&callback](std::string &utf8Char)
                                  {
                                      callback(utf8Char);
                                  });

    LlmStreamBuffer stream_buffer{[&processor](const char *str, size_t len)
                                  {
                                      processor.processStream(str, len);
                                  }};

    std::ostream output_stream(&stream_buffer);
    impl_->m_llm->response(input_ids, &output_stream, nullptr, 0);
    while (!impl_->m_llm->stoped() && !m_stop)
    {
        impl_->m_llm->generate(1);
    }

    // My_Log{} << "LLM stopped.";
    m_stop = false;

    output_stream.flush();

    auto response = context->generate_str;

    // My_Log{} << response << "\n";

    return true;
}

bool MNNContext::Stop()
{
    m_stop = true;
    return true;
}

json MNNContext::HandleProfile()
{
    // TODO: Why was it deleted?
//    std::string profile = impl_->m_llm->get_statistics();
    return {};

    std::string jsonStr;
    if (jsonStr.empty())
        return {};

    json j = json::parse(jsonStr);
    json result;

    std::ostringstream oss;

    // time_to_first_token
    oss << std::fixed << std::setprecision(2) << get_json_value(j, "prefill_time", 0.0);
    result["time_to_first_token"] = oss.str();

    // token_generation_time
    oss.str("");
    oss << std::fixed << std::setprecision(2) << get_json_value(j, "decode_time", 0.0);
    result["token_generation_time"] = oss.str();

    // prompt_processing_rate
    oss.str("");
    oss << std::fixed << std::setprecision(2) << get_json_value(j, "prefill_speed", 0.0);
    result["prompt_processing_rate"] = oss.str();

    // token_generation_rate
    oss.str("");
    oss << std::fixed << std::setprecision(2) << get_json_value(j, "decode_speed", 0.0);
    result["token_generation_rate"] = oss.str();

    // integer values
    result["num_prompt_tokens"] = get_json_value(j, "prompt_tokens", 0.0);
    result["num_generated_tokens"] = get_json_value(j, "decode_tokens", 0.0);

    return result;
}
