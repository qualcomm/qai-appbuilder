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

#define GENIE_BUILDER_DEBUG 1

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
    self->cur_length_ += context->TokenLength(response);

    if (self->cur_length_ >= self->max_length_)
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
            prompt_ = std::move(model_input.text_);
            this->BuildTextEmbedding()
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
            .BuildTextEmbedding()
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

IEmbedding &IEmbedding::BuildInferredBuffer(const QNNEmbedding::InferResource &infer_resource,
                                            std::vector<float> &in_buf,
                                            std::vector<uint8_t> &inferred_buf)
{
    static std::string perfProfile = "burst";
    auto &app_builder = infer_resource.app_builder_;
    auto mode_type = qnn_embedding_info_.model_types_;
    std::vector<uint8_t *> inputBuffers;
    int offset;
    if (mode_type & ModelType::Audio)
    {
        inputBuffers.resize(infer_resource.bin_stacks_.size());
        offset = 0;
    }
    else
    {
        offset = 1;
        inputBuffers.resize(infer_resource.bin_stacks_.size() + 1);
        inputBuffers[0] = reinterpret_cast<uint8_t *>(in_buf.data());
    }

    for (auto i = 0; i < infer_resource.bin_stacks_.size(); ++i)
    {
        inputBuffers[i + offset] = const_cast<uint8_t *>(infer_resource.bin_stacks_[i].data());
    }

    std::vector<uint8_t *> outputBuffers;
    std::vector<size_t> outputSize;
    if (!app_builder->ModelInference(infer_resource.tag_,
                                     inputBuffers,
                                     outputBuffers,
                                     outputSize,
                                     perfProfile))
    {
        throw std::runtime_error("call model inference failed");
    }

    in_buf.clear();
    inferred_buf.assign(outputBuffers.at(0), outputBuffers.at(0) + outputSize.at(0));
    for (auto &outputBuffer: outputBuffers)
        free(outputBuffer);
    return *this;
}

IEmbedding &QInterfaceImpl::IVisionEmbedding::CustomBuild(ModelInput &model_input)
{
    dynamic_cast<IVisionEmbedding &>(Decode(model_input.image_, img_buf_))
            .BuildImgPixel()
            .BuildInferredBuffer(infer_resource_,
                                 img_pixel_buf_,
                                 img_inferred_buf_)
            .BuildPrompt(kPromptTemplate, model_input.text_);
    return *this;
}

IEmbedding &QInterfaceImpl::IAudioEmbedding::CustomBuild(ModelInput &model_input)
{
    dynamic_cast<IAudioEmbedding &>(Decode(model_input.audio_, audio_buf_))
            .BuildAudioSamples()
            .BuildInferredBuffer(infer_resource_,
                                 audio_sample_buf_,
                                 audio_inferred_buf_)
            .BuildPrompt(kPromptTemplate, model_input.text_);
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
