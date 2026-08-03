//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include <stb_image.h>
#include <stb_image_resize2.h>

#include "qwen_3_vl.h"
#include "qwen3_vl_image_processor.hpp"
#include "../../torch_helper/masked_scatter.h"

IVisionEmbedding &QInterface::Qwen3VL::BuildImgPixel()
{
    using namespace qwen3_vl;
    int rows = 0, cols = 0;
    Qwen3VLImageProcessor proc;
    proc.ProcessToBuffer(img_buf_.data(), img_buf_.size(), kHeight, kWidth, img_pixel_buf_, rows, cols);
    img_buf_.clear();
    return *this;
}

IEmbedding &QInterface::Qwen3VL::CustomBuild(ModelInput &model_input)
{
    dynamic_cast<IVisionEmbedding &>(Decode(model_input.image_, img_buf_))
            .BuildImgPixel()
            .PaddingVisionPrompt()
            .BuildVisionInferredInput();
    // BuildInferredBuffer() 是 IEmbedding 的 protected 成员，IEmbedding 又是
    // IVisionEmbedding 的虚基类；不能通过上面链式调用返回的 IVisionEmbedding&
    // 访问它（C++ 虚基类保护成员访问规则），必须通过 this（Qwen3VL*）单独调用。
    BuildInferredBuffer(infer_resource_, input_buffers_, img_inferred_buffers_, deepstack_buffers_);
    return *this;
}

IVisionEmbedding &QInterface::Qwen3VL::MergeEmbedding()
{
    static const int32_t image_token_id{151655};
    const unsigned long token_count = prompt_token_size_;
    BufferView<float> tmp_raw_fbuf{qnn_embedding_info_.embedded_raw_buf_};

    std::vector<float> embedded_raw_fbuf;
    embedded_raw_fbuf.resize(token_count * cols_);
    float *dest_ptr;
    for (uint32_t i = 0; i < prompt_token_size_; ++i)
    {
        dest_ptr = &embedded_raw_fbuf[i * cols_];
        float *src_ptr = &tmp_raw_fbuf.pointer_[prompt_token_[i] * cols_];
        std::memcpy(dest_ptr, src_ptr, cols_ * sizeof(float));
    }

    if (img_inferred_buffers_.empty())
    {
        embedded_bin_ = std::move(embedded_raw_fbuf);
        input_data_ = reinterpret_cast<uint8_t*>(embedded_bin_.data());
        input_len_ = embedded_bin_.size() * sizeof(float);
        return *this;
    }

    BufferView<float> img_embedding_fbuf{img_inferred_buffers_[0]};
    std::vector<size_t> image_rows;
    torch_helper::MaskedScatterMergeEmbedding(prompt_token_, token_count, image_token_id,
                                              embedded_raw_fbuf, img_embedding_fbuf, embedded_bin_, &image_rows);

    // GenieDialog/GenieNode 均不支持向已编译好的解码器中间层注入具名张量（见
    // qwen3_vl.md），embeddingQuery 唯一可控的通道是这段扁平输入 embedding；
    // 因此把 3 层 deepstack 残差近似折算进 image_rows 对应的行再一次性传入。
    if (!deepstack_buffers_.empty())
    {
        std::vector<BufferView<float>> deepstack_fbufs;
        deepstack_fbufs.reserve(deepstack_buffers_.size());
        for (auto &buf: deepstack_buffers_)
        {
            deepstack_fbufs.emplace_back(buf);
        }
        torch_helper::AddDeepstackResidual(image_rows, static_cast<size_t>(cols_), deepstack_fbufs, embedded_bin_);
    }

    input_data_ = reinterpret_cast<uint8_t*>(embedded_bin_.data());
    input_len_ = embedded_bin_.size() * sizeof(float);
    return *this;
}
