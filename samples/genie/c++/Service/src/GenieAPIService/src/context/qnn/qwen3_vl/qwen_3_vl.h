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
                          "%s"  //<|vision_start|><|image_pad|><|vision_end|>
                          "<|im_end|>\n"
                          "<|im_start|>assistant\n";

        kPaddedList_ = "<|vision_start|><|image_pad|><|vision_end|>";

        // 图像尺寸/patch/merge 参数在 4B、8B 上完全一致（均为 512x512），
        // 仅 HIDDEN_SIZE（=VISION_OUT_HIDDEN_SIZE）随模型规模变化，见
        // ai-hub-models qwen3_vl_{4b,8b}_instruct/model.py 的架构常量对比。
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

    // 真机实测（vision_encoder.bin 图要求 Expected: 5 个输入，见 qwen3_vl.md）证实
    // get_visual_input_names() 里的 "mask" 并不是真实导出图的独立输入；这里仍覆写
    // CustomBuild() 只是为了改用能捕获额外输出（deepstack_buffers_）的 4 参
    // BuildInferredBuffer() 重载，流程与基类 IVisionEmbedding::CustomBuild() 完全一致。
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

#endif //QWEN_3_VL_H
