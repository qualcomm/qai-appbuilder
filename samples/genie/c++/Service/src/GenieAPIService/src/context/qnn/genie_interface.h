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

#define GENIE_BUILDER_DEBUG 1

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
                context_{context},
                kContextSize_{context->model_config_.context_size()} {}

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

        int cur_length_{0};

        const int kContextSize_{};

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

        std::string BuildPrompt(const std::string &system,
                                const std::string &user,
                                const std::string &padded_prompt);

    protected:
        std::string kPromptTemplate{};
        std::string padded_prompt_{};

        int32_t *prompt_token_{};
        uint32_t prompt_token_size_{};

        std::vector<float> embedded_bin_;
        const QNNEmbedding &qnn_embedding_info_;
        int cols_{};

        const QNNEmbedding::InferResource *get_infer_resource(ModelType mode_type)
        {
            auto it = qnn_embedding_info_.infer_resources_.find(mode_type);
            return (it != qnn_embedding_info_.infer_resources_.end()) ? &(it->second) : nullptr;
        }

        static std::string GeneratePaddingPrompt(const std::string &bos,
                                                 const std::string &eos,
                                                 const std::string &repeated,
                                                 int times);


        IEmbedding &Decode(std::string &encode_buf, std::vector<uint8_t> &decoded_buf);

        IEmbedding &BuildInferredBuffer(const QNNEmbedding::InferResource *infer_resource,
                                        std::vector<std::vector<uint8_t *>> &input_buffers,
                                        std::vector<std::vector<uint8_t>> &inferred_buf);

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
            padded_prompt_.clear();
            return *this;
        };

        virtual IEmbedding &MergeEmbedding() = 0;

    private:
        static void TokenToEmbedCallback(int32_t token,
                                         void *embedding,
                                         uint32_t embeddingSize,
                                         const void *userData);

        IEmbedding &BuildTextEmbedding(const std::string &completed_prompt)
        {
            if (!context_->GenerateTextToken(completed_prompt,
                                             const_cast<const int32_t *&>(prompt_token_),
                                             prompt_token_size_))
            {
                throw std::runtime_error("generate text token failed");
            }
            return *this;
        }
    };

    class IVisionEmbedding : virtual public IEmbedding
    {
    public:
        explicit IVisionEmbedding(GenieContext *context)
                : IEmbedding(context),
                  infer_resource_{get_infer_resource(ModelType{ModelType::Vision})}
        {
            if (infer_resource_ && !infer_resource_->bin_stacks_.empty())
            {
                input_buffers_.resize(1);
                input_buffers_[0].resize(infer_resource_->bin_stacks_.size() + 1);
                for (auto i = 0; i < infer_resource_->bin_stacks_.size(); ++i)
                {
                    input_buffers_[0][i + 1] = const_cast<uint8_t *>(infer_resource_->bin_stacks_[i].data());
                }
            }
        }

        ~IVisionEmbedding() override = default;

        IEmbedding &CustomBuild(ModelInput &model_input) override;

        IEmbedding &CustomClean() override
        {
            img_buf_.clear();
            img_pixel_buf_.clear();
            img_inferred_buffers_.clear();
            Clean();
            return *this;
        }

        virtual IVisionEmbedding &PaddingVisionPrompt()
        {
            padded_prompt_ += kPaddedList_;
            return *this;
        }

        virtual IVisionEmbedding &BuildVisionInferredInput()
        {
            input_buffers_[0][0] = reinterpret_cast<uint8_t *>(img_pixel_buf_.data());
            return *this;
        }

        virtual IVisionEmbedding &BuildImgPixel() = 0;

        virtual IVisionEmbedding &Clean() { return *this; }

    protected:
        int kWidth{};
        int kHeight{};

        int token_index_{};
        std::string kPaddedList_;
        std::vector<uint8_t> img_buf_{};
        std::vector<float> img_pixel_buf_{};
        std::vector<std::vector<uint8_t *>> input_buffers_{};
        std::vector<std::vector<uint8_t>> img_inferred_buffers_{};
        const QNNEmbedding::InferResource *infer_resource_;
    };

    class IAudioEmbedding : virtual public IEmbedding
    {
    public:
        explicit IAudioEmbedding(GenieContext *context)
                : IEmbedding(context),
                  infer_resource_{get_infer_resource(ModelType{ModelType::Audio})} {}

        ~IAudioEmbedding() override = default;

        IEmbedding &CustomBuild(ModelInput &model_input) override;

        IEmbedding &CustomClean() override
        {
            audio_buf_.clear();
            audio_sample_buf_.clear();
            audio_inferred_buf_.clear();
            CleanAudio();
            return *this;
        }

        virtual IAudioEmbedding &CleanAudio() = 0;

        virtual IAudioEmbedding &BuildAudioInferredInput() = 0;

        virtual IAudioEmbedding &BuildAudioSamples() = 0;

        virtual IAudioEmbedding &PaddingAudioPrompt() = 0;

        int token_index_{};
        std::vector<uint8_t> audio_buf_;
        std::vector<float> audio_sample_buf_;
        std::vector<std::vector<uint8_t *>> input_buffers_{};
        std::vector<std::vector<uint8_t>> audio_inferred_buf_;
        const QNNEmbedding::InferResource *infer_resource_;
    };

    class IMultiModal : public IAudioEmbedding, public IVisionEmbedding
    {
    public:
        explicit IMultiModal(GenieContext *context)
                : IAudioEmbedding(context), IVisionEmbedding(context) {}

        IEmbedding &CustomBuild(ModelInput &model_input) final;

        IEmbedding &CustomClean() override
        {
            IVisionEmbedding::CustomClean();
            IAudioEmbedding::CustomClean();
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
