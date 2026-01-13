//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef GENIE_IMPL_H
#define GENIE_IMPL_H

class GenieContext::Impl
{
public:
    class ConfigFixer
    {
    public:
        struct FixedInfo;

        explicit ConfigFixer(const IModelConfig &model_config) : model_config_{model_config}
        {}

        json Execute();

    private:
        bool FixedPath(json &j, FixedInfo &info);

        const IModelConfig &model_config_;
    };

    class QInterface
    {
    public:
        virtual ~QInterface() = default;

        static QInterface *CreateInterface(GenieContext *context);

        explicit QInterface(GenieContext *context) :
            context_{context}
        {}

        virtual bool set_content(const std::string &prompt) = 0;

        Genie_Status_t GenieDialogQuery()
        {
            const auto status = GenieDialogQueryImpl();
            prompt_data_ = nullptr;
            prompt_len_ = 0;
            return status;
        }

        virtual Genie_Status_t GenieDialogQueryImpl() = 0;

    protected:
        GenieContext *context_;

        uint8_t *prompt_data_;
        uint32_t prompt_len_;

        static void GenieCallBack(const char *response,
                                  GenieDialog_SentenceCode_t sentence_code,
                                  const void *user_data);
    };

    class IGeneral : public QInterface
    {
    public:
        explicit IGeneral(GenieContext *context) : QInterface(context)
        {}

        bool set_content(const std::string &prompt) final
        {
            prompt_data_ = reinterpret_cast<uint8_t *>(const_cast<char *>(prompt.data()));
            prompt_len_ = prompt.size();
#ifdef GENIE_BUILDER_DEBUG
            My_Log{} << "\n[Prompt]:\n"
                    << content.prompt_ << "\n------------\n\n"
                    << "[Response]:\n";
#endif
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
        static IEmbedding *CreateInterface(GenieContext *context)
        {
            switch (context->model_config_.get_qnn_embedded_info().type_.v_)
            {
                case QNNEmbeddingType::PHI4MM:
                    return new PHI4Embedding(context);
                case QNNEmbeddingType::QWEN2_5:
                    return new Qwen2_5Embedding(context);
            }

            return nullptr;
        }

        explicit IEmbedding(GenieContext *context) :
            QInterface(context),
            embedded_raw_buf_{context_->model_config_.get_embedded_raw_buffer()},
            qnn_embedding_info_{context->model_config_.get_qnn_embedded_info()}
        {}

        bool set_content(const std::string &prompt) final;

        Genie_Status_t GenieDialogQueryImpl() final
        {
            img_buf_.clear();
            img_pixel_buf_.clear();
            img_embedding_buf_.clear();
            question_embedding_buf_.clear();
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

    protected:
        std::vector<uint8_t> img_buf_;
        std::vector<float> img_pixel_buf_;
        std::vector<uint8_t> img_embedding_buf_;
        std::vector<int32_t> question_embedding_buf_;
        std::vector<float> embedded_bin_;
        const std::vector<uint8_t> &embedded_raw_buf_;
        const QNNEmbeddingInfo &qnn_embedding_info_;
        int kWidth{};
        int kHeight{};

        std::function<std::string(std::string &&prompt)> pre_process_prompt_{};

        IEmbedding &DecodeImg(std::string &&img_hash);

        virtual IEmbedding &BuildImgPixel() = 0;

        IEmbedding &BuildPixelEmbeddings();

        virtual IEmbedding &BuildTextEmbedding(const std::string &question);

        virtual std::vector<float> MergeEmbedding() = 0;

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
    };

    class PHI4Embedding : public IEmbedding
    {
    public:
        explicit PHI4Embedding(GenieContext *context) : IEmbedding(context)
        {
            kWidth = 448;
            kHeight = 364;
        }

        IEmbedding &BuildImgPixel() final;

        IEmbedding &BuildTextEmbedding(const std::string &question) final;

        std::vector<float> MergeEmbedding() final;
    };

    class Qwen2_5Embedding : public IEmbedding
    {
    public:
        explicit Qwen2_5Embedding(GenieContext *context) : IEmbedding(context)
        {
            pre_process_prompt_ = [](std::string &&prompt)
            {
                static const std::string prompt_template{
                    "<|im_start|>system\n"
                    "You are a helpful assistant.<|im_end|>\n"
                    "<|im_start|>user\n"
                    "<|vision_start|><|image_pad|><|vision_end|>%s<|im_end|>\n"
                    "<|im_start|>assistant\n"
                };

                std::string buf(prompt_template.size() + prompt.size() + 1, 0);
                sprintf(buf.data(), prompt_template.c_str(), prompt.c_str());
                return buf;
            };
            kHeight = kWidth = 640;
        }

        IEmbedding &BuildImgPixel() final;

        std::vector<float> MergeEmbedding() final;
    };

    QInterface *interface_{};
    int CurLength{0};
    int MaxLength{4096};
};

#endif //GENIE_IMPL_H
