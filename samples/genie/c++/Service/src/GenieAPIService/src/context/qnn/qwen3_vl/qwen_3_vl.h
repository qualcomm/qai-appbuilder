//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef QWEN_3_VL_H
#define QWEN_3_VL_H

#include "../genie_interface.h"

class QInterface::Qwen3VL : public IVisionEmbedding
{
public:
    explicit Qwen3VL(GenieContext *context) : IVisionEmbedding(context), IEmbedding(context)
    {
        kPromptTemplate = "<|im_start|>system\n"
                          "%s.<|im_end|>\n"
                          "<|im_start|>user\n%s"
                          "%s"  //<|vision_start|><|image_pad|><|vision_end|>
                          "<|im_end|>\n"
                          "<|im_start|>assistant\n";

        kPaddedList_ = "<|vision_start|><|image_pad|><|vision_end|>";

        kHeight = kWidth = 768;
        cols_ = 2560;

        token_to_embed_callback_fn_ = &TokenToEmbedCallback<float, float>;
    }

    IVisionEmbedding &BuildImgPixel() final;

    IVisionEmbedding &MergeEmbedding() override;

    IVisionEmbedding &CleanVision() override
    {
        embedded_bin_.clear();
        return *this;
    }

    std::vector<float> embedded_bin_;
};

#endif //QWEN_3_VL_H
