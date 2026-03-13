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
#include "utils.h"

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

        auto &name = context->model_config_.get_model_name();
        if (str_contains(name, "7b"))
        {
            kHeight = 342;
            kWidth = 512;
            cols_ = 3584;
        }
        else if (str_contains(name, "3b"))
        {
            kHeight = kWidth = 640;
            cols_ = 2048;
        }
        else
        {
            throw std::runtime_error("Qwen2_5 not match rules of 7b or 3b");
        }
    }

    IVisionEmbedding &BuildImgPixel() final;

    IVisionEmbedding &MergeEmbedding() override;
};

#endif //QWEN_2_5_H
