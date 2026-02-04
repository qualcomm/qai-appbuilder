//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef GENIE_IMPL_H
#define GENIE_IMPL_H

#include "genie.h"

struct GenieContext::QInterfaceImpl
{
    QInterfaceImpl(GenieContext *context)
    {
        if (context->model_config_.get_qnn_embedding().embedding_type_ == QNNEmbeddingType::None)
        {
            inf_ = new IGeneral{context};
            return;
        }

        if (!(inf_ = IEmbedding::CreateInterface(context)))
        {
            throw std::runtime_error("create embedding interface failed");
        }
    }

    ~QInterfaceImpl()
    {
        if (inf_)
        {
            delete inf_;
            inf_ = nullptr;
        }
    }

    class QInterface
    {
    public:
        virtual ~QInterface() = default;

        explicit QInterface(GenieContext *context) :
                context_{context} {}

        virtual bool set_content(ModelInput &model_input) = 0;

        Genie_Status_t GenieDialogQuery()
        {
            const auto status = GenieDialogQueryImpl();
            prompt_data_ = nullptr;
            prompt_len_ = 0;
            return status;
        }

        virtual Genie_Status_t GenieDialogQueryImpl() = 0;

        static void OutPutText(ModelInput &model_input);

        int max_length_{DEFAULT_CONTEXT_SIZE};

        int cur_length_{0};

        class PHI4Embedding;

        class Qwen2_5;

        class Qwen2_5OMINI;

    protected:
        GenieContext *context_;
        uint8_t *prompt_data_{};
        uint32_t prompt_len_{};

        static void GenieCallBack(const char *response,
                                  GenieDialog_SentenceCode_t sentence_code,
                                  const void *user_data);
    };

    class IGeneral : public QInterface
    {
    public:
        explicit IGeneral(GenieContext *context) : QInterface(context) {}

        bool set_content(ModelInput &model_input) final
        {
            prompt_data_ = reinterpret_cast<uint8_t *>(const_cast<char *>(model_input.text_.data()));
            prompt_len_ = model_input.text_.size();
            OutPutText(model_input);
            return true;
        }

        Genie_Status_t GenieDialogQueryImpl() final
        {
            return GenieDialog_query(context_->m_DialogHandle,
                                     reinterpret_cast<const char *>(prompt_data_),
                                     GENIE_DIALOG_SENTENCE_COMPLETE,
                                     GenieCallBack,
                                     this);
        }
    };

    class IEmbedding : public QInterface
    {
    public:
        static IEmbedding *CreateInterface(GenieContext *context);

        IEmbedding(GenieContext *context);

        ~IEmbedding() override = default;

        bool set_content(ModelInput &model_input) final;

        Genie_Status_t GenieDialogQueryImpl() final
        {
            auto rs = GenieDialog_embeddingQuery(context_->m_DialogHandle,
                                                 prompt_data_,
                                                 prompt_len_,
                                                 GENIE_DIALOG_SENTENCE_COMPLETE,
                                                 TokenToEmbedCallback,
                                                 GenieCallBack,
                                                 this);
            embedded_bin_.clear();
            return rs;
        }

        IEmbedding &BuildPrompt(const std::string &prompt_template, const std::string &input)
        {
            prompt_.reserve(prompt_template.size() + input.size());
            sprintf(prompt_.data(), prompt_template.c_str(), input.c_str());
            return *this;
        }

    protected:
        int32_t *prompt_token_{};
        uint32_t prompt_token_size_{};
        std::vector<float> embedded_bin_;
        const QNNEmbedding &qnn_embedding_info_;
        std::string prompt_;

        IEmbedding &Decode(std::string &encode_buf, std::vector<uint8_t> &decoded_buf);

        IEmbedding &BuildInferredBuffer(const QNNEmbedding::InferResource &infer_resource,
                                        std::vector<float> &in_buf,
                                        std::vector<uint8_t> &inferred_buf);

        virtual IEmbedding &CustomBuild(ModelInput &model_input) = 0;

        virtual IEmbedding &CustomClean() = 0;

        IEmbedding &Clean()
        {
            CustomClean();
            if (prompt_token_)
            {
                free(prompt_token_);
                prompt_token_ = nullptr;
            }
            prompt_.clear();
            return *this;
        };

        virtual IEmbedding & MergeEmbedding() = 0;

        struct FloatBufferView
        {
            explicit FloatBufferView(const std::vector<uint8_t> &buffer)
            {
                pointer_ = reinterpret_cast<float *>(const_cast<std::vector<uint8_t> &>(buffer).data());
                size_ = buffer.size() / sizeof(float);
            }

            float *pointer_{};
            unsigned long size_{};
        };

    private:
        static void TokenToEmbedCallback(int32_t token,
                                         void *embedding,
                                         uint32_t embeddingSize,
                                         const void *userData);

        IEmbedding &BuildTextEmbedding()
        {
            if (!context_->GenerateTextToken(prompt_, const_cast<const int32_t *&>(prompt_token_), prompt_token_size_))
            {
                throw std::runtime_error("generate text token failed");
            }
            prompt_.clear();
            return *this;
        }
    };

    class IVisionEmbedding : virtual public IEmbedding
    {
    public:
        explicit IVisionEmbedding(GenieContext *context)
                : IEmbedding(context),
                  infer_resource_(qnn_embedding_info_.infer_resources_.at(ModelType{ModelType::Vision})) {};

        ~IVisionEmbedding() override = default;

        IEmbedding &CustomBuild(ModelInput &model_input) override;

        IEmbedding &CustomClean() override
        {
            img_buf_.clear();
            img_pixel_buf_.clear();
            img_inferred_buf_.clear();
            return *this;
        }

        virtual IVisionEmbedding &BuildImgPixel() = 0;

    protected:
        int kWidth{};
        int kHeight{};

        std::string kPromptTemplate;
        std::vector<uint8_t> img_buf_{};
        std::vector<float> img_pixel_buf_{};
        std::vector<uint8_t> img_inferred_buf_{};
        const QNNEmbedding::InferResource &infer_resource_;
    };

    class IAudioEmbedding : virtual public IEmbedding
    {
    public:
        explicit IAudioEmbedding(GenieContext *context) :
                IEmbedding(context),
                infer_resource_(qnn_embedding_info_.infer_resources_.at(ModelType{ModelType::Audio})) {}

        ~IAudioEmbedding() override = default;

        IEmbedding &CustomBuild(ModelInput &model_input) override;

        IEmbedding &CustomClean() override
        {
            audio_buf_.clear();
            audio_sample_buf_.clear();
            audio_inferred_buf_.clear();
            return *this;
        }

        virtual IAudioEmbedding &BuildAudioSamples() = 0;

        std::string kPromptTemplate;
        std::vector<uint8_t> audio_buf_;
        std::vector<float> audio_sample_buf_;
        std::vector<uint8_t> audio_inferred_buf_;
        const QNNEmbedding::InferResource &infer_resource_;
    };

    class IMultiModal : virtual public IAudioEmbedding, public IVisionEmbedding
    {
    public:
        explicit IMultiModal(GenieContext *context) : IAudioEmbedding(context), IVisionEmbedding(context) {}

        IEmbedding &CustomBuild(ModelInput &model_input) final;

        IEmbedding &CustomClean() override
        {
            IAudioEmbedding::CustomClean();
            IVisionEmbedding::CustomClean();
            return *this;
        }
    };

    QInterface *inf_;
};

using QInterfaceImpl = GenieContext::QInterfaceImpl;
using QInterface = GenieContext::QInterfaceImpl::QInterface;
using IEmbedding = QInterfaceImpl::IEmbedding;
using IVisionEmbedding = QInterfaceImpl::IVisionEmbedding;
using IAudioEmbedding = QInterfaceImpl::IAudioEmbedding;
using IMultiModal = QInterfaceImpl::IMultiModal;

#endif //GENIE_IMPL_H
