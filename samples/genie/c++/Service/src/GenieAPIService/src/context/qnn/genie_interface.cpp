//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "genie.h"
#include "genie_interface.h"
#include "log.h"
#include "utils.h"
#include "base64.h"
#include <LibAppBuilder.hpp>

#include "phi4mm/phi4mm.h"
#include "qwen2_5/qwen_2_5.h"
#include "qwen2_5_omini/qwen_2_5_omini.h"

IEmbedding::IEmbedding(GenieContext *context) :
        QInterface(context),
        qnn_embedding_info_{context->model_config_.get_qnn_embedding()} {}

void QInterface::OutPutText(ModelInput &model_input)
{
#ifdef GENIE_BUILDER_DEBUG
    My_Log{} << "\n[Prompt]:\n"
             << model_input.text_ << "\n------------\n\n"
             << "[Response]:\n";
#endif
}

void QInterfaceImpl::QInterface::GenieCallBack(const char *response,
                                               const GenieDialog_SentenceCode_t sentence_code,
                                               const void *user_data)
{
    auto *self = static_cast<QInterface *>(const_cast<void *>(user_data));
    auto *context = self->context_;
    if (sentence_code == GENIE_DIALOG_SENTENCE_END
        || !response
        || !strlen(response))
    {
        return;
    }

    std::lock_guard guard(context->m_stream_lock);
    context->m_stream_answer += response;
    context->m_stream_cond.notify_one();  // Notify waiting thread that new data is available
    self->cur_length_ += context->TokenLength(response);
    if (self->cur_length_ >= self->kContextSize_)
    {
        My_Log{My_Log::Level::kError} << "g_CurLength: " << self->cur_length_ << "is over and will stop self"
                                      << std::endl;
        context->Stop();
    }
}

IEmbedding *IEmbedding::CreateInterface(GenieContext *context)
{
    switch (int(context->model_config_.get_qnn_embedding().embedding_type_))
    {
        case QNNEmbeddingType::PHI4MM:
            return new PHI4Embedding(context);
        case QNNEmbeddingType::QWEN2_5:
            return new Qwen2_5(context);
        case QNNEmbeddingType::QWEN2_5_OMINI:
            return new Qwen2_5OMINI(context);
    }
    return nullptr;
}

bool IEmbedding::set_content(ModelInput &model_input)
{
    auto model_type = qnn_embedding_info_.model_types_;
    if (model_input.image_.empty() && model_input.audio_.empty())
    {
        if (model_type & ModelType::Text)
        {
            OutPutText(model_input);
            this->BuildTextEmbedding(model_input.text_)
                .MergeEmbedding();
            goto ahead;
        }
        else
        {
            throw ReportError{"not supprot text mode\n"};
        }
    }

    if (!model_input.image_.empty())
    {
        if (!(model_type & ModelType::Vision))
        {
            throw ReportError{"not supprot vision mode\n"};
        }
    }

    if (!model_input.audio_.empty())
    {
        if (!(model_type & ModelType::Audio))
        {
            throw ReportError{"not supprot audio mode\n"};
        }
    }
    try
    {
        this->CustomBuild(model_input)
            .BuildTextEmbedding(BuildPrompt(model_input.system_, padded_prompt_, model_input.text_))
            .MergeEmbedding()
            .Clean();
    }
    catch (std::exception &e)
    {
        My_Log{My_Log::Level::kError} << "set content failed: " << e.what() << "\n";
        Clean();
        return false;
    }

    ahead:
    prompt_data_ = reinterpret_cast<uint8_t *>(embedded_bin_.data());
    prompt_len_ = embedded_bin_.size() * sizeof(float);
    return true;
}

void IEmbedding::TokenToEmbedCallback(const int32_t token,
                                      void *embedding,
                                      const uint32_t embeddingSize,
                                      const void *userData)
{
    auto *self = static_cast<IEmbedding *>(const_cast<void *>(userData));
    const size_t lutIndex = token * embeddingSize;
    if (lutIndex + embeddingSize <= self->qnn_embedding_info_
                                        .embedded_raw_buf_
                                        .size())
    {
        const int8_t *embeddingSrc = reinterpret_cast<const int8_t *>(self->qnn_embedding_info_
                                                                          .embedded_raw_buf_
                                                                          .data()) + lutIndex;
        auto *embeddingDst = static_cast<int8_t *>(embedding);
        std::copy(embeddingSrc, embeddingSrc + embeddingSize, embeddingDst);
    }
    else
    {
        My_Log{My_Log::Level::kError} << "Error: T2E conversion overflow.\n";
    }
}

std::string QInterfaceImpl::IEmbedding::GeneratePaddingPrompt(const std::string &bos,
                                                              const std::string &eos,
                                                              const std::string &repeated,
                                                              int times)
{
    std::string prompt;
    int needed = times * repeated.length() + bos.length() + eos.length();
    prompt.reserve(needed + 1); // for \0
    prompt.append(bos);
    for (int i = 0; i < times; ++i)
    {
        prompt.append(repeated);
    }
    prompt.append(eos);
    return prompt;
}

IEmbedding &QInterfaceImpl::IEmbedding::Decode(std::string &encode_buf, std::vector<uint8_t> &decoded_buf)
{
    decoded_buf.resize(BASE64_DECODE_OUT_SIZE(encode_buf.size()));
    if (Base64Decode(encode_buf.data(), encode_buf.size(), decoded_buf.data()) == 0)
    {
        encode_buf.clear();
        throw std::runtime_error("decode to binrary failed");
    }
    encode_buf.clear();
    return *this;
}

IEmbedding &IEmbedding::BuildInferredBuffer(const QNNEmbedding::InferResource *infer_resource,
                                            std::vector<std::vector<uint8_t *>> &input_buffers,
                                            std::vector<std::vector<uint8_t>> &inferred_buffers)
{
    static std::string perfProfile = "burst";
    auto app_builder = infer_resource->app_builder_;
    std::vector<uint8_t *> outputBuffers;
    std::vector<size_t> outputSize;

    inferred_buffers.resize(input_buffers.size());
    for (auto i = 0; i < input_buffers.size(); ++i)
    {
        if (!app_builder->ModelInference(infer_resource->tag_,
                                         input_buffers[i],
                                         outputBuffers,
                                         outputSize,
                                         perfProfile))
        {
            throw std::runtime_error("call model inference failed");
        }
        inferred_buffers[i].assign(outputBuffers.at(0), outputBuffers.at(0) + outputSize.at(0));
        free(outputBuffers[0]);
        outputSize.clear();
        outputBuffers.clear();
    }
    return *this;
}

std::string QInterfaceImpl::IEmbedding::BuildPrompt(const std::string &system,
                                                    const std::string &user,
                                                    const std::string &padded_prompt)
{
    std::string completed_prompt;
    completed_prompt.reserve(kPromptTemplate.size() + padded_prompt.size() + system.size() + user.size() + 1);
    completed_prompt[completed_prompt.size()] = '\0';
    sprintf(completed_prompt.data(), kPromptTemplate.data(), system.c_str(), user.c_str(), padded_prompt.data());
    My_Log(completed_prompt.c_str(), My_Log::Level::kInfo);
    return completed_prompt;
}

IEmbedding &QInterfaceImpl::IVisionEmbedding::CustomBuild(ModelInput &model_input)
{
    dynamic_cast<IVisionEmbedding &>(Decode(model_input.image_, img_buf_))
            .BuildImgPixel()
            .PaddingVisionPrompt()
            .BuildVisionInferredInput()
            .BuildInferredBuffer(infer_resource_,
                                 input_buffers_,
                                 img_inferred_buffers_);
    return *this;
}

IEmbedding &QInterfaceImpl::IAudioEmbedding::CustomBuild(ModelInput &model_input)
{
    dynamic_cast<IAudioEmbedding &>(Decode(model_input.audio_, audio_buf_))
            .BuildAudioSamples()
            .PaddingAudioPrompt()
            .BuildAudioInferredInput()
            .BuildInferredBuffer(infer_resource_,
                                 input_buffers_,
                                 audio_inferred_buf_);
    return *this;
}

IEmbedding &IMultiModal::CustomBuild(ModelInput &model_input)
{
    if (!model_input.audio_.empty())
    {
        IAudioEmbedding::CustomBuild(model_input);
    }

    if (!model_input.image_.empty())
    {
        IVisionEmbedding::CustomBuild(model_input);
    }
    return *this;
}
