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

struct FloatBufferView;

class QInterface::Qwen2_5OMINI : public IMultiModal
{
public:
    explicit Qwen2_5OMINI(GenieContext *context) : IMultiModal(context),
                                                   IEmbedding(context)
    {
        kPromptTemplate = "<|im_start|>system\n"
                          "%s<|im_end|>\n"
                          "<|im_start|>user\n%s"
                          "%s" // <|audio_bos|><|IMAGE|><|audio_eos|> and <|vision_bos|><|AUDIO|><|vision_eos|>
                          "<|im_end|>\n<|im_start|>assistant\n";

        cols_ = 2048;
        IVisionEmbedding::token_index_ = 196;
        kPaddedList_ = GeneratePaddingPrompt("<|vision_bos|>",
                                             "<|vision_eos|>",
                                             "<|IMAGE|>",
                                             IVisionEmbedding::token_index_
        );
        kHeight = kWidth = 384;

        IAudioEmbedding::input_buffers_.resize(1);
        IAudioEmbedding::input_buffers_[0].resize(3);
    }

    IAudioEmbedding &PaddingAudioPrompt() final
    {
        padded_prompt_ += GeneratePaddingPrompt("<|audio_bos|>",
                                                "<|audio_eos|>",
                                                "<|AUDIO|>",
                                                IAudioEmbedding::token_index_);
        return *this;
    }

    IAudioEmbedding &BuildAudioSamples() override;

    IAudioEmbedding &BuildAudioInferredInput() override;

    void MergeImpl(int token_index, int standard_index, std::vector<uint8_t> &inferred_buf);

    IAudioEmbedding &CleanAudio() override
    {
        IAudioEmbedding::token_index_ = 0;
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
