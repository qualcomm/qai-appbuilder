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
    torch_helper::MaskedScatterMergeEmbedding(prompt_token_, token_count, image_token_id,
                                              embedded_raw_fbuf, img_embedding_fbuf, embedded_bin_);

    input_data_ = reinterpret_cast<uint8_t*>(embedded_bin_.data());
    input_len_ = embedded_bin_.size() * sizeof(float);
    return *this;
}
