//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef QWEN_2_5_OMINI_H
#define QWEN_2_5_OMINI_H

#include "../genie_interface.h"

class QInterface::Qwen2_5OMINI : public IMultiModal
{
public:
    explicit Qwen2_5OMINI(GenieContext *context) : IMultiModal(context),
                                                   IEmbedding(context)
    {
        const char *kRawPromptTemplate{"<|im_start|>system\n"
                                    "You are Qwen, a virtual human developed by the Qwen Team, Alibaba Group, capable of perceiving auditory and visual inputs, as well as generating text and speech.<|im_end|>\n"
                                    "<|im_start|>user\n%s"
                                    "<|vision_bos|>%s<|vision_eos|>"
                                    "<|audio_bos|>%s<|audio_eos|>"
                                    "<|im_end|>\n<|im_start|>assistant\n"};
        cols_ = 2048;
        IVisionEmbedding::token_index = 196;
        kHeight = kWidth = 384;

        std::string image_list;
        int len = IVisionEmbedding::token_index * strlen("<|IMAGE|>");
        image_list.reserve(len + 1);
        for (int i = 0; i < IVisionEmbedding::token_index; ++i)
        {
            image_list.append("<|IMAGE|>");
        }
        kPromptTemplate.resize(strlen(kRawPromptTemplate) + image_list.size());
        sprintf(kPromptTemplate.data(), kRawPromptTemplate, "%s", image_list.c_str(), "%s");

        IAudioEmbedding::input_buffers_.resize(3);
    }

    IAudioEmbedding &BuildAudioSamples() override;

    IAudioEmbedding &PaddingAudioPrompt() override;

    IAudioEmbedding &BuildAudioInferredInput() override;

    void MergeImpl(int token_index, int standard_index, std::vector<uint8_t> &inferred_buf);

    IAudioEmbedding &CleanAudio() override
    {
        padded_feature_.clear();
        padded_mask_.clear();
        padded_attention_mask_.clear();
        return *this;
    }

    IEmbedding &MergeEmbedding() override;

    IVisionEmbedding &BuildImgPixel() override;

private:
    static inline const int kPaddingMaxLength = 4800000;
    std::vector<float> padded_feature_;
    std::vector<float> padded_mask_;
    std::vector<float> padded_attention_mask_;

    std::vector<std::vector<float>> ProcessingMelSpec(std::vector<std::vector<float>> &&mel_spec);

    std::vector<std::vector<float>> ApplyMaskTimeMajorToMelMajor(
            std::vector<std::vector<float>> &&time_major_mel_spec,
            const std::vector<int32_t> &mask);

    std::vector<int64_t> CreateChunkLengths(int64_t feature_len);

    std::vector<std::vector<std::vector<float>>> SplitMelMajorByLengths(
            std::vector<std::vector<float>> &&input_features,
            const std::vector<int64_t> &chunk_lengths);

    std::tuple<std::vector<float>, std::vector<float>> MakePaddedAndMask(
            const std::vector<std::vector<std::vector<float>>> &tensor_list,
            const std::vector<int64_t> &tensor_len
    );

    std::vector<float> PaddedInputFeatures(
            std::vector<float> &&padded_feature_flat, // size == N * C * T, layout (N, C, T)
            size_t N, size_t C, size_t T);

    std::vector<std::vector<std::vector<float>>> MakeMask3DFromFlat(
            std::vector<float> &&batch_mask_flat,
            size_t N,
            size_t L);

    std::vector<float> MakeUpdatedPaddedMaskFrom3D(
            const std::vector<std::vector<std::vector<float>>> &padded_mask, // [N][1][T]
            size_t max_len);

    std::vector<std::vector<int32_t>> MakeBatchMaskAfterCnn(const std::vector<int64_t> &tensor_len);

    std::vector<int32_t> ComputeCuSeqlensFromMask(const std::vector<std::vector<int32_t>> &mask);

    std::vector<float> AttentionToPadded(
            std::vector<float> &&attention_mask, // size == 1*H*H
            size_t H,
            std::vector<int32_t> &cu_seqlens,
            size_t P_H = 1000,
            size_t P_W = 1000);

    void MaskedScatter(
            const std::vector<uint8_t> &mask,
            const FloatBufferView &audio_features);

    size_t idx3(size_t n, size_t i, size_t j, size_t H, size_t W)
    {
        return (n * H + i) * W + j;
    }
};

#endif //QWEN_2_5_OMINI_H
