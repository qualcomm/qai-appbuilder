//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef QWEN_2_5_H
#define QWEN_2_5_H

#include "../genie_interface.h"
class QInterface::Qwen2_5 : public IVisionEmbedding
{
public:
    explicit Qwen2_5(GenieContext *context) : IVisionEmbedding(context), IEmbedding(context)
    {
        kPromptTemplate = "<|im_start|>system\n"
                          "You are a helpful assistant.<|im_end|>\n"
                          "<|im_start|>user\n"
                          "<|vision_start|><|image_pad|><|vision_end|>%s<|im_end|>\n"
                          "<|im_start|>assistant\n";
        kHeight = kWidth = 640;
    }

    IVisionEmbedding &BuildImgPixel() final;

    IVisionEmbedding & MergeEmbedding() override;
};

#endif //QWEN_2_5_H
