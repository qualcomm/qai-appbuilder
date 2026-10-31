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
#include "utils.h"

class QInterface::Qwen3VL : public IVisionEmbedding
{
public:
    explicit Qwen3VL(GenieContext *context) : IVisionEmbedding(context), IEmbedding(context)
    {
        kPromptTemplate = "<|im_start|>system\n"
                          "%s<|im_end|>\n"
                          "<|im_start|>user\n%s"
                          "%s"
                          "<|im_end|>\n"
                          "<|im_start|>assistant\n";

        kPaddedList_ = "<|vision_start|><|image_pad|><|vision_end|>";

        kHeight = kWidth = 512;

        auto &name = context->model_config_.get_model_name();
        if (str_contains(name, "8b"))
        {
            cols_ = 4096;
        }
        else if (str_contains(name, "4b"))
        {
            cols_ = 2560;
        }
        else
        {
            throw std::runtime_error("Qwen3VL not match rules of 4b or 8b");
        }

        token_to_embed_callback_fn_ = &TokenToEmbedCallback<float, float>;
    }

    IVisionEmbedding &BuildImgPixel() final;

    IEmbedding &CustomBuild(ModelInput &model_input) override;

    IVisionEmbedding &MergeEmbedding() override;

    IVisionEmbedding &CleanVision() override
    {
        embedded_bin_.clear();
        deepstack_buffers_.clear();
        return *this;
    }

    std::vector<float> embedded_bin_;
    std::vector<std::vector<uint8_t>> deepstack_buffers_;
};

#endif
