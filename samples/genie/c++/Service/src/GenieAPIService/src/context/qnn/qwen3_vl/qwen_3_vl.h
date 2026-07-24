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

        kHeight = kWidth = 512;
        cols_ = 2560;

        token_to_embed_callback_fn_ = &TokenToEmbedCallback<float, float>;
    }

    IVisionEmbedding &BuildImgPixel() final;

    // Qwen3-VL 的 mask 输入依赖 prompt_token_ 中 image_token_id 的位置（见
    // _shared/lm_driver/qwen3_vl.py:246-247 get_visual_input_names()），而
    // prompt_token_ 只有在 BuildTextEmbedding() 执行之后才可用；但基类的
    // IVisionEmbedding::CustomBuild() 会在 BuildTextEmbedding() 之前就调用
    // BuildVisionInferredInput()+BuildInferredBuffer() 触发视觉编码器推理。
    // 因此这里覆写 CustomBuild()，只做 Decode+BuildImgPixel+PaddingVisionPrompt，
    // 把 BuildVisionInferredInput()/BuildInferredBuffer() 推迟到 MergeEmbedding()
    // （此时 prompt_token_ 已经就绪）再执行。
    IEmbedding &CustomBuild(ModelInput &model_input) override;

    IVisionEmbedding &BuildVisionInferredInput() override;

    IVisionEmbedding &MergeEmbedding() override;

    IVisionEmbedding &CleanVision() override
    {
        embedded_bin_.clear();
        mask_buf_.clear();
        deepstack_buffers_.clear();
        return *this;
    }

    std::vector<float> embedded_bin_;
    std::vector<uint8_t> mask_buf_;
    std::vector<std::vector<uint8_t>> deepstack_buffers_;
};

#endif //QWEN_3_VL_H
