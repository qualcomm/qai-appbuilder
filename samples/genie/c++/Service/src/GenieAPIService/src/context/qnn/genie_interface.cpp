//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "genie.h"
#include "genie_interface.h"
#include <cstdio>

template<>
void IEmbedding::TokenToEmbedCallback<float, float>(int32_t token,
                                                    void *embedding,
                                                    uint32_t embeddingSize,
                                                    const void *userData);

#include "log.h"
#include "utils.h"
#include "base64.h"
#include <LibAppBuilder.hpp>

#include "phi4mm/phi4mm.h"
#include "qwen2_5/qwen_2_5.h"
#include "qwen2_5_omini/qwen_2_5_omini.h"
#include "qwen3_vl/qwen_3_vl.h"

IEmbedding::IEmbedding(GenieContext *context) :
        QInterface(context),
        qnn_embedding_info_{context->model_config_.i_model_config_.get_qnn_embedding()} {}

void QInterface::OutPutText(ModelInput &model_input, const char *query_type_label)
{
    My_Log{} << "\n[Prompt] [" << query_type_label << "][" << context_->get_model_name() << "]:\n"
             << model_input.text_ << "\n------------\n\n"
             << "[Response] [" << query_type_label << "][" << context_->get_model_name() << "]:\n";
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
        // 我们自己按分词估算主动喊停(区别于 genie.cpp 里 SDK 自己报告
        // GENIE_STATUS_WARNING_CONTEXT_EXCEEDED 的那一条 [MODEL_DEFECT] 日志:
        // 那条是 SDK 内部真实上下文耗尽,这一条是我们自己的估算提前达到上限)。
        // 同样以 error 级别打一条带 [MODEL_DEFECT] 标记、reason 字段不同的日志,
        // 供 test_service.py 侧区分识别、归类为模型缺陷诊断信息。
        My_Log{My_Log::Level::kError} << "[MODEL_DEFECT] reason=self_estimated_limit_exceeded token_size="
                                      << self->cur_length_ << " is over and will stop self" << std::endl;
        context->stopped_by_output_limit_ = true;
        context->Stop();
    }
}

IEmbedding *IEmbedding::CreateInterface(GenieContext *context)
{
    switch (int(context->model_config_.i_model_config_.get_qnn_embedding().embedding_type_))
    {
        case QNNEmbeddingType::PHI4MM:
            return new PHI4Embedding(context);
        case QNNEmbeddingType::QWEN2_5:
            return new Qwen2_5(context);
        case QNNEmbeddingType::QWEN2_5_OMINI:
            return new Qwen2_5OMINI(context);
        case QNNEmbeddingType::QWEN3_VL:
            return new Qwen3VL(context);
    }
    return nullptr;
}

bool IEmbedding::set_content(ModelInput &model_input)
{
    My_Log{} << model_input.image_.empty() << " " << model_input.audio_.empty() << "\n";
    auto model_type = qnn_embedding_info_.model_types_;
    if (model_input.image_.empty() && model_input.audio_.empty())
    {
        if (model_type & ModelType::Text)
        {
            OutPutText(model_input, context_->get_query_type_label(model_input));
            this->BuildTextEmbedding(model_input.text_)
                .MergeEmbedding();
            goto ahead;
        }
        else
        {
            throw ReportError{"not support text mode\n"};
        }
    }
    if (!model_input.image_.empty())
    {
        if (!(model_type & ModelType::Vision))
        {
            throw ReportError{"not support vision mode\n"};
        }
    }

    if (!model_input.audio_.empty())
    {
        if (!(model_type & ModelType::Audio))
        {
            throw ReportError{"not support audio mode\n"};
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
    return true;
}

template<>
void IEmbedding::TokenToEmbedCallback<float, float>(const int32_t token,
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

template<typename T, typename P>
void IEmbedding::TokenToEmbedCallback(const int32_t token,
                                      void *embedding,
                                      const uint32_t embeddingSize,
                                      const void *userData)
{
    auto *self = static_cast<IEmbedding *>(const_cast<void *>(userData));
    size_t num_elements = embeddingSize / sizeof(P);
    size_t lutIndex = static_cast<size_t>(token) * num_elements;

    if ((lutIndex + num_elements) * sizeof(T) <= self->qnn_embedding_info_
                                                     .embedded_raw_buf_
                                                     .size())
    {
        const T *embeddingSrc = static_cast<const T *>(self->qnn_embedding_info_
                                                           .embedded_raw_buf_
                                                           .data()) + (lutIndex);
        P *embeddingDst = static_cast<P *>(embedding);
        for (size_t i = 0; i < num_elements; i++)
        {
            embeddingDst[i] = static_cast<P>(self->requant_scale * embeddingSrc[i] + self->requant_offset);
        }
    }
    else
    {
        My_Log{My_Log::Level::kError} << "Error: T2E conversion overflow.\n";
    }
}

Genie_Status_t QInterfaceImpl::IEmbedding::GenieDialogQueryImpl()
{
    auto rs = GenieDialog_embeddingQuery(context_->m_DialogHandle,
                                         input_data_,
                                         input_len_,
                                         GENIE_DIALOG_SENTENCE_COMPLETE,
                                         token_to_embed_callback_fn_,
                                         GenieCallBack,
                                         this);
    if (context_->model_config_.i_model_config_.get_qnn_embedding().embedding_type_ == QNNEmbeddingType::QWEN3_VL)
    {
        My_Log("[Qwen3VL DIAG] GenieDialog_embeddingQuery returned rs=" + std::to_string(int(rs))
               + " input_len_=" + std::to_string(input_len_)
               + " stream_answer_len=" + std::to_string(context_->m_stream_answer.size()),
               My_Log::Level::kWarning);
    }
    return rs;
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
    auto needed = std::snprintf(nullptr, 0, kPromptTemplate.c_str(), system.c_str(), user.c_str(), padded_prompt.c_str());
    std::string completed_prompt;
    completed_prompt.resize(needed + 1);
    std::snprintf(completed_prompt.data(), completed_prompt.size(), kPromptTemplate.c_str(), system.c_str(), user.c_str(), padded_prompt.c_str());
    completed_prompt.resize(needed);
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
