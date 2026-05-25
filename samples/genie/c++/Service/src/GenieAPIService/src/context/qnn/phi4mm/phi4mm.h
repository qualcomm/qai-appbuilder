//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef PHI_4_EMBEDDING_H
#define PHI_4_EMBEDDING_H

#include "../../torch_helper/base.h"
#include "../genie_interface.h"

Shape_4D<float> repeat_shape4d(const Shape_4D_View<float> &in, int r0, int r1, int r2, int r3);

class Image;

class QInterface::PHI4Embedding : public IVisionEmbedding
{
public:
    explicit PHI4Embedding(GenieContext *context) : IVisionEmbedding(context), IEmbedding(context)
    {
        kPromptTemplate = "<|system|>%s<|end|>\n"
                          "<|user|>%s%s<|end|><|assistant|>";

        kWidth = 448;
        kHeight = 448;

        BufferView<float> view{infer_resource_->tails_bin_stacks_[0]};
        std::vector<float> buf{view.pointer_, view.pointer_ + view.size_};
        glb_gn_ = {1, 1, C, buf};

        BufferView<float> view1{infer_resource_->tails_bin_stacks_[1]};
        sub_gn_ = {1, 1, 1, C, view1.pointer_, static_cast<int>(view1.size_)};
        glb_gn_repeat_ = repeat_shape4d(sub_gn_, 1, H, 1, 1);
        cols_ = 8192;

        switch (int(qnn_embedding_info_.data_type))
        {
            case EmbeddingDataType::INT8:
                ep_ = new EmbeddingProcess<uint16_t>(this);
                token_to_embed_callback_fn_ = &TokenToEmbedCallback<uint8_t, uint16_t>;
                requant_scale = LUT8_SCALE / BASE16_SCALE;
                requant_offset = requant_scale * LUT8_OFFSET - BASE16_OFFSET;
                break;
            case EmbeddingDataType::FLOAT32:
                ep_ = new EmbeddingProcess<float>(this);
                token_to_embed_callback_fn_ = &TokenToEmbedCallback<float, float>;
                break;
        }
    }

    ~PHI4Embedding();

    IVisionEmbedding &PaddingVisionPrompt() final
    {
        padded_prompt_ += GeneratePaddingPrompt("",
                                                "",
                                                "<|endoftext10|>",
                                                token_index_);
        return *this;
    }

    IVisionEmbedding &BuildImgPixel() final;

    IVisionEmbedding &BuildVisionInferredInput() override
    {
        input_buffers_.reserve(valid_crops_);
        for (auto i = 0; i < valid_crops_; ++i)
        {
            input_buffers_.push_back({reinterpret_cast<uint8_t *>(crop_pixels_[i].buf.data()),
                                      reinterpret_cast<uint8_t *>(crop_position_ids_[i].buf.data()),
                                      reinterpret_cast<uint8_t *>(crop_attention_mask_[i].buf.data())});
        }
        return *this;
    }

    IVisionEmbedding &MergeEmbedding() final;

    IVisionEmbedding &CleanVision() final
    {
        crop_h_ = 0;
        crop_w_ = 0;

        useful_width_ = 0;
        useful_height_ = 0;

        token_index_ = 0;
        valid_crops_ = 0;
        crop_pixels_.clear();
        crop_position_ids_.clear();
        crop_attention_mask_.clear();
        input_buffers_.clear();
        ep_->Clean();
        return *this;
    }

private:
    std::pair<Image, Shape_2D<float>> DynamicPreprocess();

    std::vector<std::pair<int, int>> GenerateTargetRatios();

    Shape_4D<float> GenerateGlobalImg(const Image &img);

    Shape_2D<int64_t> compute_position_ids(
            const Shape_4D<float> &image_transformed,
            const Shape_3D<uint8_t> &patch_attention_mask);

    Shape_3D<float> Compose();

    const int C = 3072;
    const int DynamicHD = 9;
    const int kMaskSize = 32;
    const int H = 16;
    const int kPatchSize = 14;
    const int SPECIAL_TOKEN_ID = 200010;
    const float LUT8_SCALE = 0.018504901960784314, BASE16_SCALE = 0.0022296844981610775;
    const int LUT8_OFFSET = -131, BASE16_OFFSET = -30985;

    int valid_crops_;
    int crop_h_{};
    int crop_w_{};

    int useful_height_{};
    int useful_width_{};

    Shape_3D<float> glb_gn_;
    Shape_4D_View<float> sub_gn_;
    Shape_4D<float> glb_gn_repeat_;
    std::vector<Shape_4D<float>> crop_pixels_;
    std::vector<Shape_4D<float>> crop_attention_mask_;
    std::vector<Shape_2D<float>> crop_position_ids_;

    struct IEmbeddingProcessBase
    {
        IEmbeddingProcessBase(PHI4Embedding *parent) : parent{parent} {}

        virtual ~IEmbeddingProcessBase() = default;

        virtual void Clean() = 0;

        PHI4Embedding *parent;

        virtual IVisionEmbedding &MergeEmbeddingImpl(std::vector<float> &embedded_bin) = 0;
    };

    template<typename T>
    struct EmbeddingProcess : IEmbeddingProcessBase
    {
        EmbeddingProcess(PHI4Embedding *parent) : IEmbeddingProcessBase{parent} {};

        std::vector<T> embedded_bin_;

        void Clean() final { embedded_bin_.clear(); }

        IVisionEmbedding &MergeEmbeddingImpl(std::vector<float> &embedded_bin) final;
    };

    IEmbeddingProcessBase *ep_{};
};

#endif //PHI_4_EMBEDDING_H
